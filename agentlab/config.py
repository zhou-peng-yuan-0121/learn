"""全局配置中心。

所有环境变量、默认值、配置校验都在这个文件里——
无论是服务端、CLI 还是测试，都从同一个 Config 对象取数据。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

# 项目根目录 —— 确保无论从哪个目录启动都能找到
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    """不可变的全局配置。"""

    # ----- LLM -----
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    request_timeout: float = 60.0
    max_retries: int = 3

    # ----- Embedding -----
    # 留空 = 用内置的离线 HashingEmbedder（不花钱、确定性、可测）；
    # 填了模型名且配了 key = 走 OpenAI 兼容的 /embeddings 接口。
    embed_model: str = ""

    # ----- HTTP 服务 -----
    host: str = "127.0.0.1"
    port: int = 7860

    # ----- 日志 -----
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> "Config":
        """从环境变量读取配置，给默认值兜底。"""
        env = os.environ if source is None else source

        # 辅助：取环境变量，如果为 None 或空字符串则用 default
        def pick(name: str, default: str) -> str:
            value = env.get(name)
            return value if isinstance(value, str) and value.strip() else default

        # 辅助：取整数（端口、重试次数）
        def pick_int(name: str, default: int) -> int:
            try:
                return int(env.get(name, default))
            except (ValueError, TypeError):
                return default

        # 允许的日志级别集合：DEBUG / INFO / WARNING / ERROR
        import logging
        log_level = pick("AGENTLAB_LOG_LEVEL", "INFO").upper()
        if log_level not in logging._nameToLevel:
            log_level = "INFO"

        return cls(
            base_url=pick("AGENTLAB_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            api_key=pick("AGENTLAB_API_KEY", ""),
            model=pick("AGENTLAB_MODEL", "deepseek-chat"),
            embed_model=pick("AGENTLAB_EMBED_MODEL", ""),
            host=pick("AGENTLAB_HOST", "127.0.0.1"),
            port=pick_int("AGENTLAB_PORT", 7860),
            log_level=log_level,
        )

    @property
    def is_configured(self) -> bool:
        """API Key 填了没？—— 没填就只能在 FakeLLM 模式下跑。"""
        return bool(self.api_key)

    def with_overrides(self, **kwargs: Any) -> "Config":
        """返回一个替换了部分字段的副本。

        为什么不用 ``config.log_level = "DEBUG"``？
        因为 ``frozen=True`` 的 dataclass 被设计成不可变——这是刻意的：
        一个模块改了配置不应该污染另一个模块。要改就造新对象。
        """
        return replace(self, **kwargs)

    def data_dir(self):
        """返回数据目录路径，并在首次访问时自动创建。"""
        d = PROJECT_ROOT / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


def load_dotenv_if_available() -> None:
    """如果项目根有 .env 文件，用最朴素的方式加载到 os.environ。

    为什么不用 python-dotenv？—— 零依赖是我们的底线。
    为什么只读等号两边没有引号的？—— 够用了。
    """
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.is_file():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value