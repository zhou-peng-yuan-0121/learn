"""测试辅助工具。

为什么要自己管临时目录？
``tempfile.mkdtemp`` 在某些 Windows 沙箱环境下创建出来的目录带 DENY ACL，
连文件都写不进去（SQLite 会报 "unable to open database file"）。
所以这里改成在项目内 ``.tmp/`` 下用唯一名字 ``mkdir``，用完再尽力删除。
"""

from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


def new_temp_dir(case: unittest.TestCase, label: str = "case") -> Path:
    """为一个测试用例创建独占的临时目录，并在用例结束后清理。"""
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_TMP_ROOT / f"{label}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
