"""零依赖 HTTP 服务：把前面所有模块接到浏览器上。

## 为什么用 http.server 而不是 Flask/FastAPI

- 零依赖，用户 clone 下来直接跑；
- 只有一个页面 + 几个 JSON 接口，框架的复杂度收益不明显；
- **手写一遍才知道框架帮你做了什么**：路由匹配、请求体解析、
  Content-Type、流式响应、错误处理。

## 关键设计

1. **每个请求开一个数据库连接**：SQLite 的连接不能跨线程用
   （ThreadingHTTPServer 每个请求一个线程），所以按请求创建、用完关闭。
   连接建立是微秒级，完全可接受。
2. **响应统一序列化**：业务函数只返回 Python 对象，序列化在响应层做。
3. **聊天走 SSE**：Agent 每一步完成就推一条事件，前端逐条渲染，
   用户能看到「它在检索什么、检索到了什么」，而不是干等 30 秒。

## 路由

    GET  /              前端页面
    GET  /api/stats     库统计
    GET  /api/docs      文档列表
    GET  /api/search    检索（不调模型）
    POST /api/ingest    录入文本或网页
    POST /api/chat      对话（SSE 流式）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .agent import AgentStep, ReActAgent
from .builtin_tools import default_registry
from .config import Config
from .embedding import make_embedder
from .ingest import IngestError, IngestPipeline, default_store
from .knowledge_tools import format_hits, make_knowledge_tools
from .retrieval import HybridRetriever

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class App:
    """整个服务共享的依赖。"""

    config: Config
    embedder: Any
    llm: Any | None = None

    @classmethod
    def create(cls, config: Config) -> "App":
        from .llm import make_llm

        llm = None
        if config.is_configured:
            try:
                llm = make_llm(config)
            except Exception:  # key 配了但不可用，别让整个服务起不来
                logger.exception("初始化模型客户端失败，将以纯检索模式运行")
        else:
            logger.warning("未配置 API key，对话接口将只返回检索结果")

        return cls(config=config, embedder=make_embedder(config), llm=llm)


def make_handler(app: App, static_dir: Path = STATIC_DIR) -> type[BaseHTTPRequestHandler]:
    """生成绑定了依赖的 Handler 类（工厂模式：不用全局变量传状态）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "agentlab/0.1"

        # ---------- 基础设施 ----------

        def log_message(self, fmt: str, *args: Any) -> None:  # 用统一日志而非 stderr
            logger.info("HTTP %s", fmt % args)

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json({"error": message}, status)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("Content-Length 不合法")
            if length <= 0:
                return {}
            if length > 2 * 1024 * 1024:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        def _sse_open(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _sse(self, payload: dict[str, Any]) -> None:
            line = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
            self.wfile.flush()

        # ---------- 路由 ----------

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(self._route_get)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch(self._route_post)

        def _dispatch(self, route: Callable[[], None]) -> None:
            try:
                route()
            except BrokenPipeError:
                logger.debug("客户端提前断开")
            except Exception as exc:  # 兜底：任何异常都变成 500，别让线程崩掉
                logger.exception("请求处理失败")
                try:
                    self._send_error_json(500, f"服务器内部错误：{exc}")
                except Exception:
                    pass

        def _route_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/stats":
                return self._api_stats()
            if path == "/api/docs":
                return self._api_docs(query)
            if path == "/api/search":
                return self._api_search(query)
            return self._send_error_json(404, f"未知路径：{path}")

        def _route_post(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/ingest":
                return self._api_ingest()
            if path == "/api/chat":
                return self._api_chat()
            return self._send_error_json(404, f"未知路径：{path}")

        # ---------- 静态页面 ----------

        def _serve_index(self) -> None:
            index = static_dir / "index.html"
            if not index.is_file():
                return self._send_bytes(b"index.html missing", "text/plain; charset=utf-8", 500)
            self._send_bytes(index.read_bytes(), "text/html; charset=utf-8")

        # ---------- 只读接口 ----------

        def _api_stats(self) -> None:
            store = default_store(app.config)
            try:
                self._send_json(store.stats())
            finally:
                store.close()

        def _api_docs(self, query: dict[str, list[str]]) -> None:
            limit = _int_param(query, "limit", 20, minimum=1, maximum=200)
            store = default_store(app.config)
            try:
                documents = [
                    {
                        "id": doc.id,
                        "title": doc.title,
                        "source": doc.source,
                        "created_at": doc.created_at,
                        "chunk_count": doc.chunk_count,
                    }
                    for doc in store.list_documents(limit)
                ]
            finally:
                store.close()
            self._send_json({"documents": documents})

        def _api_search(self, query: dict[str, list[str]]) -> None:
            text = (query.get("q") or [""])[0].strip()
            if not text:
                return self._send_error_json(400, "缺少参数 q")
            top_k = _int_param(query, "top_k", 5, minimum=1, maximum=20)
            mode = (query.get("mode") or ["hybrid"])[0]
            if mode not in ("hybrid", "keyword", "vector"):
                return self._send_error_json(400, f"未知 mode：{mode}")

            store = default_store(app.config)
            try:
                retriever = HybridRetriever(store, app.embedder)
                hits = retriever.search(text, top_k=top_k, mode=mode)
            finally:
                store.close()
            self._send_json({"query": text, "mode": mode, "hits": [hit.as_dict() for hit in hits]})

        # ---------- 写入接口 ----------

        def _api_ingest(self) -> None:
            try:
                payload = self._read_json()
            except ValueError as exc:
                return self._send_error_json(400, str(exc))

            text = (payload.get("text") or "").strip()
            url = (payload.get("url") or "").strip()
            title = (payload.get("title") or "").strip() or None
            if not text and not url:
                return self._send_error_json(400, "text 和 url 至少要给一个")

            store = default_store(app.config)
            try:
                pipeline = IngestPipeline(store, app.embedder)
                try:
                    result = pipeline.ingest_url(url) if url else pipeline.ingest_text(text, title=title)
                except IngestError as exc:
                    return self._send_error_json(400, str(exc))
            finally:
                store.close()

            self._send_json(
                {
                    "document_id": result.document_id,
                    "title": result.title,
                    "chunk_count": result.chunk_count,
                    "char_count": result.char_count,
                    "source": result.source,
                }
            )

        # ---------- 对话接口（SSE） ----------

        def _api_chat(self) -> None:
            try:
                payload = self._read_json()
            except ValueError as exc:
                return self._send_error_json(400, str(exc))

            question = str(payload.get("question") or "").strip()
            if not question:
                return self._send_error_json(400, "question 不能为空")

            self._sse_open()
            store = default_store(app.config)
            try:
                retriever = HybridRetriever(store, app.embedder)

                # 没有 key：退化成「纯检索」，让用户仍然能验证整条链路
                if app.llm is None:
                    self._sse({"type": "note", "text": "未配置 API key：下面是纯检索结果（配置后可让 Agent 生成答案）"})
                    hits = retriever.search(question, top_k=5)
                    self._sse({"type": "final", "answer": format_hits(hits), "usage": {}})
                    return

                registry = default_registry()
                for spec in make_knowledge_tools(retriever).specs():
                    registry.add(spec)
                agent = ReActAgent(app.llm, registry)

                def on_step(step: AgentStep) -> None:
                    self._sse(
                        {
                            "type": "step",
                            "index": step.index,
                            "kind": step.kind,
                            "thought": step.thought,
                            "tool": step.tool,
                            "arguments": step.arguments,
                            "observation": step.observation,
                        }
                    )

                result = agent.run(question, on_step=on_step)
                self._sse(
                    {
                        "type": "final",
                        "answer": result.answer,
                        "stop_reason": result.stop_reason,
                        "usage": result.usage.as_dict(),
                    }
                )
            except Exception as exc:
                logger.exception("对话处理失败")
                try:
                    self._sse({"type": "error", "message": str(exc)})
                except Exception:
                    pass
            finally:
                store.close()

    return Handler


def _int_param(
    query: dict[str, list[str]],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """安全地读整数查询参数：非法值回落默认值，并夹在范围内。"""
    raw = (query.get(name) or [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


class KnowledgeServer:
    """把 ThreadingHTTPServer 包成可测试、可优雅关闭的对象。"""

    def __init__(
        self,
        config: Config,
        *,
        host: str | None = None,
        port: int | None = None,
        app: App | None = None,
    ) -> None:
        self.app = app or App.create(config)
        address = (host or config.host, port if port is not None else config.port)
        self._httpd = ThreadingHTTPServer(address, make_handler(self.app))
        self._httpd.daemon_threads = True
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def start_background(self) -> "KnowledgeServer":
        """在后台线程跑起来——测试和脚本里很方便。"""
        import threading

        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return self

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "KnowledgeServer":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.shutdown()
