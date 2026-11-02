"""命令行入口 —— ``python -m agentlab`` 的统一跳板。

目前只有 ``--help``，后续每实现一个功能就往这里面加子命令。
"""

from __future__ import annotations

import argparse
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

    logger.warning("命令 %r 尚未实现", args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())