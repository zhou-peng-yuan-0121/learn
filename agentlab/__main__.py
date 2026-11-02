"""命令行入口 —— ``python -m agentlab`` 的统一跳板。

目前只有 ``--help``，后续每实现一个功能就往这里面加子命令。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .config import Config, load_dotenv_if_available
from .logging_config import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    """构建子命令解析器。"""
    parser = argparse.ArgumentParser(
        prog="agentlab",
        description="AI 个人知识库 Agent —— 把日常信息变成可对话的 second brain",
    )
    parser.add_argument("--version", action="version", version=f"agentlab {__version__}")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default=None, help="覆盖配置文件中的日志级别")

    sub = parser.add_subparsers(dest="command", title="可用命令")

    demo = sub.add_parser("demo-llm", help="用 FakeLLM 演示流式输出（不需要 API key）")
    demo.add_argument("--speed", type=float, default=0.02,
                      help="每个片段之间的间隔秒数，默认 0.02")

    sub.add_parser("show-tools", help="打印工具清单和自动生成的 JSON Schema")

    demo_agent = sub.add_parser("demo-agent", help="用 FakeLLM 演示完整的 ReAct 循环（不需要 API key）")
    demo_agent.add_argument("--max-steps", type=int, default=6)

    ask = sub.add_parser("ask", help="用真实模型跑一个任务（需要配置 API key）")
    ask.add_argument("question", help="你要问的问题")
    ask.add_argument("--max-steps", type=int, default=6, help="最多迭代步数")

    ingest = sub.add_parser("ingest", help="把一段文字或一个网页录入知识库")
    source = ingest.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="要录入的文本")
    source.add_argument("--url", help="要抓取并录入的网页地址")
    ingest.add_argument("--title", help="文档标题（不给则取第一行/网页标题）")

    docs = sub.add_parser("docs", help="列出知识库里的文档")
    docs.add_argument("--limit", type=int, default=20)

    search = sub.add_parser("search", help="检索知识库（不调用大模型，纯检索）")
    search.add_argument("query", help="检索词")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--mode", choices=["hybrid", "keyword", "vector"], default="hybrid")
    search.add_argument("--explain", action="store_true", help="显示每条结果来自哪一路")

    # -- 未来的子命令都会加在这里 --
    # sub.add_parser("serve", help="启动本地 HTTP 服务 / Web UI")
    # sub.add_parser("ingest", help="录入一篇知识")
    # sub.add_parser("chat", help="终端对话模式")
    # sub.add_parser("search", help="检索已有知识")

    return parser


def run_demo_llm(speed: float) -> int:
    """用假模型演示「流式输出」长什么样。

    为什么要做成命令而不是测试？—— 因为**看得见**。
    测试证明逻辑对，demo 让人直观理解「流式」和「非流式」的区别。
    """
    from .llm import FakeLLM  # 延迟导入：--help 不需要加载 llm 模块

    text = "你好，我是 FakeLLM。你现在看到的这段文字，是一个片段一个片段到达终端的——这就是流式输出。"
    llm = FakeLLM([text], chunk_size=2)
    messages = [{"role": "user", "content": "演示一下流式输出"}]

    print("模型：", end="", flush=True)
    for piece in llm.stream(messages):
        sys.stdout.write(piece)
        sys.stdout.flush()  # 关键：不 flush 会被缓冲区攒着，看不出「流」的效果
        time.sleep(max(0.0, speed))
    print("\n")
    print(f"（共 {len(text)} 个字，切成 {len(range(0, len(text), 2))} 个片段到达）")
    return 0


def print_steps(result) -> None:
    """把 AgentResult 的轨迹打印成人类可读的时间线。"""
    print(f"\n=== 执行轨迹（{len(result.steps)} 步，{result.usage.total_tokens} tokens）===")
    for step in result.steps:
        print(f"[{step.index}] 思考：{step.thought or '（无）'}")
        if step.kind == "final":
            print(f"    最终：{step.observation}")
        elif step.tool:
            args = ", ".join(f"{k}={v!r}" for k, v in step.arguments.items())
            print(f"    行动：{step.tool}({args})")
            print(f"    观察：{step.observation}")
        elif step.kind == "error":
            print(f"    错误：{step.observation}")
    print(f"\n=== 结果 ===\n{result.answer or '（无答案）'}")
    print(f"结束原因：{result.stop_reason}")


def run_demo_agent(max_steps: int) -> int:
    """用假模型跑一遍完整循环——不联网也能看懂 Agent 在干什么。"""
    from .agent import ReActAgent
    from .builtin_tools import default_registry
    from .llm import FakeLLM

    script = [
        json.dumps(
            {"thought": "要算 12*34+56，先调用计算器",
             "action": {"tool": "calculator", "args": {"expression": "12*34+56"}}},
            ensure_ascii=False,
        ),
        json.dumps(
            {"thought": "计算器返回了 464，可以回答了",
             "final": "12×34+56 = 464"},
            ensure_ascii=False,
        ),
    ]

    agent = ReActAgent(FakeLLM(script), default_registry(), max_steps=max_steps)
    print("用户：帮我算 12*34+56")
    result = agent.run("帮我算 12*34+56")
    print_steps(result)
    print("\n提示：这次用的是脚本化的 FakeLLM，所以轨迹完全可复现；")
    print("      配置好 API key 后用 `agentlab ask \"问题\"` 接真实模型。")
    return 0 if result.ok else 1


def run_ask(config, question: str, max_steps: int) -> int:
    """用真实模型回答一个问题（会自动挂上知识库检索工具）。"""
    from .agent import ReActAgent
    from .builtin_tools import default_registry
    from .embedding import make_embedder
    from .ingest import default_store
    from .knowledge_tools import make_knowledge_tools
    from .llm import LLMError, make_llm
    from .retrieval import HybridRetriever

    try:
        llm = make_llm(config)
    except LLMError as exc:
        print(f"无法启动：{exc}")
        print("提示：先跑 `python -m agentlab demo-agent` 看看效果（不需要 key）。")
        return 1

    # 工具表 = 通用工具 + 知识库检索工具
    registry = default_registry()
    store = default_store(config)
    try:
        retriever = HybridRetriever(store, make_embedder(config))
        for spec in make_knowledge_tools(retriever).specs():
            registry.add(spec)

        agent = ReActAgent(llm, registry, max_steps=max_steps)
        print(f"用户：{question}")
        result = agent.run(question)
    finally:
        store.close()

    print_steps(result)
    return 0 if result.ok else 1


def run_search(config, query: str, top_k: int, mode: str, explain: bool) -> int:
    """纯检索，不经过大模型——调检索效果时最快的手段。"""
    from .embedding import make_embedder
    from .ingest import default_store
    from .retrieval import HybridRetriever

    store = default_store(config)
    try:
        retriever = HybridRetriever(store, make_embedder(config))
        hits = retriever.search(query, top_k=top_k, mode=mode)
        stats = store.stats()
    finally:
        store.close()

    if not hits:
        print(f"没有找到与 {query!r} 相关的内容。（库里有 {stats['documents']} 篇文档）")
        return 0

    print(f"查询：{query}    模式：{mode}    命中：{len(hits)}\n")
    for index, hit in enumerate(hits, start=1):
        print(f"[{index}]《{hit.title}》 融合分 {hit.score:.4f}")
        if explain:
            print(f"    来源：{hit.explain()}")
        print(f"    {hit.snippet}")
        print()
    return 0


def run_show_tools() -> int:
    """打印工具清单——模型在提示词里看到的就是这份东西。"""
    from .builtin_tools import default_registry

    registry = default_registry()

    print("=== 给提示词用的紧凑清单 ===")
    print(registry.describe())

    print("\n=== 结构化 JSON Schema（可直接放进请求体的 tools 字段）===")
    print(json.dumps(registry.openai_schema(), ensure_ascii=False, indent=2))

    print("\n=== 提示 ===")
    print("这份 schema 是从函数签名自动生成的：改函数参数，这里跟着变，不需要手写。")
    return 0


def run_ingest(config, *, text: str | None, url: str | None, title: str | None) -> int:
    """录入一段文本或一个网页。"""
    from .ingest import IngestError, default_pipeline

    pipeline = default_pipeline(config)
    try:
        if url:
            print(f"正在抓取 {url} …")
            result = pipeline.ingest_url(url)
        else:
            result = pipeline.ingest_text(text or "", title=title)
    except IngestError as exc:
        print(f"摄取失败：{exc}")
        return 1
    finally:
        pipeline.store.close()

    print(result.summary())
    return 0


def run_docs(config, limit: int) -> int:
    """列出知识库中的文档。"""
    from .ingest import default_store

    store = default_store(config)
    try:
        documents = store.list_documents(limit)
        if not documents:
            print("知识库还是空的。先录一点东西：")
            print('  python -m agentlab ingest --text "今天读了一篇关于 RAG 的文章……" --title "读书笔记"')
            return 0

        print(f"{'ID':<4} {'块数':>4}  {'创建时间':<20} 标题")
        print("-" * 70)
        for doc in documents:
            print(f"{doc.id:<4} {doc.chunk_count:>4}  {doc.created_at:<20} {doc.title}")

        stats = store.stats()
        print(f"\n共 {stats['documents']} 篇 / {stats['chunks']} 块")
        print(f"数据库：{stats['path']}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """程序入口，返回退出码。"""
    # 最先加载 .env（在任何可能用到 API key 的操作之前）
    load_dotenv_if_available()

    parser = build_parser()
    args = parser.parse_args(argv)

    # 加载配置
    config = Config.from_env()

    # 日志级别可以被命令行覆盖
    if args.log_level:
        config = config.with_overrides(log_level=args.log_level)

    logger = setup_logging(config)
    logger.info("agentlab v%s 启动", __version__)
    logger.debug("base_url=%s model=%s api_key=%s",
                 config.base_url, config.model,
                 "已配置" if config.is_configured else "未配置")

    if args.command is None:
        parser.print_help()
        return 0

    # 命令分发：每个子命令对应一个函数，保持 main() 只做「解析 + 分发」
    if args.command == "demo-llm":
        return run_demo_llm(args.speed)
    if args.command == "show-tools":
        return run_show_tools()
    if args.command == "demo-agent":
        return run_demo_agent(args.max_steps)
    if args.command == "ask":
        return run_ask(config, args.question, args.max_steps)
    if args.command == "ingest":
        return run_ingest(config, text=args.text, url=args.url, title=args.title)
    if args.command == "docs":
        return run_docs(config, args.limit)
    if args.command == "search":
        return run_search(config, args.query, args.top_k, args.mode, args.explain)

    logger.warning("命令 %r 尚未实现", args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())