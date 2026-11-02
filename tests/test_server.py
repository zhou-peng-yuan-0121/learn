"""HTTP 层集成测试：起真服务、发真请求。

为什么不用 mock 掉 HTTP？
这一层最容易出问题的地方恰恰是「接线」——路由拼错、Content-Type 不对、
SSE 格式差一个空行。mock 掉就把这些全跳过了，等于没测。
真起一个服务跑在随机端口上，几毫秒的事。
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from agentlab.config import Config
from agentlab.embedding import HashingEmbedder
from agentlab.llm import FakeLLM
from agentlab.server import App, KnowledgeServer
from tests.support import new_temp_dir


def make_config(data_dir: Path) -> Config:
    return Config(
        base_url="https://api.example.com/v1",
        api_key="",
        model="test-model",
        data_dir=data_dir,
    )


def parse_sse(body: bytes) -> list[dict]:
    """把 SSE 响应体拆成事件列表。"""
    events: list[dict] = []
    for chunk in body.decode("utf-8").split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        events.append(json.loads(chunk[len("data:"):].strip()))
    return events


class ServerTestCase(unittest.TestCase):
    """每个用例起一个独立服务（随机端口 + 独立数据目录）。"""

    app_factory = None

    def setUp(self) -> None:
        self.data_dir = new_temp_dir(self, "server")
        self.config = make_config(self.data_dir)
        app = self.app_factory(self.config) if self.app_factory else None
        server = KnowledgeServer(self.config, host="127.0.0.1", port=0, app=app)
        self.server = server.start_background()
        self.addCleanup(self.server.shutdown)
        self.base = self.server.url

    # ---------- 请求辅助 ----------

    def get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=15) as response:
            return response.status, response.read()

    def get_json(self, path: str) -> dict:
        status, body = self.get(path)
        self.assertEqual(status, 200)
        return json.loads(body)

    def post(self, path: str, payload: dict) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()

    def post_json(self, path: str, payload: dict) -> dict:
        status, body = self.post(path, payload)
        self.assertEqual(status, 200)
        return json.loads(body)

    def expect_error(self, method: str, path: str, payload: dict | None = None) -> int:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=15)
        return ctx.exception.code

    # ---------- 辅助动作 ----------

    def ingest(self, text: str, title: str | None = None) -> dict:
        return self.post_json("/api/ingest", {"text": text, "title": title})


class TestStaticAndStats(ServerTestCase):
    def test_首页返回HTML(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("agentlab", body.decode("utf-8"))
        self.assertIn("知识库", body.decode("utf-8"))

    def test_统计接口(self):
        stats = self.get_json("/api/stats")
        self.assertEqual(stats["documents"], 0)
        self.assertEqual(stats["chunks"], 0)
        self.assertTrue(stats["fts"], "FTS5 应该可用")

    def test_文档列表初始为空(self):
        self.assertEqual(self.get_json("/api/docs")["documents"], [])

    def test_未知路径返回404(self):
        self.assertEqual(self.expect_error("GET", "/api/nope"), 404)


class TestIngestApi(ServerTestCase):
    def test_录入文本后能查到(self):
        result = self.ingest("RAG 的核心是检索增强生成。分块大小很关键。", title="RAG 笔记")
        self.assertEqual(result["title"], "RAG 笔记")
        self.assertGreater(result["chunk_count"], 0)

        documents = self.get_json("/api/docs")["documents"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["title"], "RAG 笔记")
        self.assertGreater(documents[0]["chunk_count"], 0)

    def test_没有标题时取第一行(self):
        result = self.ingest("第一行当标题\n这里是正文")
        self.assertEqual(result["title"], "第一行当标题")

    def test_空内容返回400(self):
        self.assertEqual(self.expect_error("POST", "/api/ingest", {}), 400)

    def test_非法JSON返回400(self):
        request = urllib.request.Request(
            self.base + "/api/ingest",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=15)
        self.assertEqual(ctx.exception.code, 400)


class TestSearchApi(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ingest("向量检索把文本映射到高维空间，用余弦相似度衡量方向一致性。", title="向量检索原理")
        self.ingest("RAG 检索增强生成的核心是先把外部知识检索出来。", title="RAG 学习笔记")

    def test_检索返回命中(self):
        data = self.get_json("/api/search?q=" + urllib.parse.quote("余弦相似度"))
        self.assertTrue(data["hits"])
        self.assertEqual(data["hits"][0]["title"], "向量检索原理")
        self.assertIn("snippet", data["hits"][0])

    def test_缺少查询参数返回400(self):
        self.assertEqual(self.expect_error("GET", "/api/search"), 400)

    def test_非法mode返回400(self):
        self.assertEqual(self.expect_error("GET", "/api/search?q=abc&mode=magic"), 400)

    def test_top_k被钳制(self):
        data = self.get_json("/api/search?q=" + urllib.parse.quote("检索") + "&top_k=999")
        self.assertLessEqual(len(data["hits"]), 20)


class TestChatWithoutKey(ServerTestCase):
    """没配 API key 时，对话接口必须优雅降级为纯检索。"""

    def test_返回SSE且降级为检索结果(self):
        self.ingest("RAG 指的是检索增强生成，先把资料检索出来再让模型回答。", title="RAG 笔记")
        status, body = self.post("/api/chat", {"question": "RAG 是什么"})

        self.assertEqual(status, 200)
        events = parse_sse(body)
        types = [event["type"] for event in events]

        self.assertIn("note", types)
        self.assertIn("final", types)
        answer = next(event["answer"] for event in events if event["type"] == "final")
        self.assertIn("RAG 笔记", answer, "降级模式下应该给出检索到的内容")

    def test_空问题返回400(self):
        self.assertEqual(self.expect_error("POST", "/api/chat", {"question": "  "}), 400)


class FakeAppFactory:
    """注入 FakeLLM 的 App，用来测「有模型时」的完整 SSE 流程。"""

    @staticmethod
    def build(config: Config) -> App:
        script = [
            json.dumps({
                "thought": "先查知识库",
                "action": {"tool": "search_knowledge", "args": {"query": "RAG"}},
            }, ensure_ascii=False),
            json.dumps({"thought": "检索到内容，可以回答", "final": "RAG 是检索增强生成。"}, ensure_ascii=False),
        ]
        return App(config=config, embedder=HashingEmbedder(dim=64), llm=FakeLLM(script))


class TestChatWithModel(ServerTestCase):
    app_factory = staticmethod(FakeAppFactory.build)

    def test_流式推送每一步(self):
        self.ingest("RAG 是检索增强生成。", title="RAG 笔记")
        status, body = self.post("/api/chat", {"question": "RAG 是什么"})

        self.assertEqual(status, 200)
        events = parse_sse(body)
        steps = [event for event in events if event["type"] == "step"]
        finals = [event for event in events if event["type"] == "final"]

        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["answer"], "RAG 是检索增强生成。")
        # 第一波应该是「调工具」，最后一波是 final
        self.assertGreaterEqual(len(steps), 2)
        self.assertEqual(steps[0]["tool"], "search_knowledge")
        self.assertIn("RAG 笔记", steps[0]["observation"])
        self.assertEqual(steps[-1]["kind"], "final")


if __name__ == "__main__":
    unittest.main(verbosity=2)
