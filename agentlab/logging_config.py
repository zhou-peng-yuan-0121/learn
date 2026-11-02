"""统一日志系统。

只做三件事：
1. 从 Config 读级别
2. 统一格式（时间 + 级别 + 模块名 + 消息）
3. 输出到终端——不为难新手，暂不写文件
"""

from __future__ import annotations

import logging
import sys

from .config import Config

_FORMATTER = logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


def setup_logging(config: Config) -> logging.Logger:
    """初始化全局日志并返回 root logger。"""
    level = logging._nameToLevel.get(config.log_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # 清理已有 handler，避免重复
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_FORMATTER)
    root.addHandler(console)

    return root


def get_logger(name: str) -> logging.Logger:
    """快捷方法：获指定名字的 logger。"""
    return logging.getLogger(name)