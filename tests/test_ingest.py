"""摄取管线测试：分块策略、HTML 清洗、抓取、端到端入库。"""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from agentlab.embedding import HashingEmbedder
from agentlab.ingest import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    IngestError,
    IngestPipeline,
    chunk_text,
    clean_html,
    fetch_url,
    normalize_text,
)
from agentlab.storage import Store
from tests.support import new_temp_dir


class FakeHTTPResponse:
    def __init__(self, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._payload = payload
        headers = Message()
        headers["Content-Type"] = content_type
        self.headers = headers

    def read(self, *args) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


# ---------------------------------------------------------------- 文本归一化与分块

class TestNormalize(unittest.TestCase):
    def test_压缩空白和多余空行(self):
        self.assertEqual(normalize_text("a  b\tc\r\n\r\n\r\nd"), "a b c\n\nd")


class TestChunkText(unittest.TestCase):
    def test_空文本返回空列表(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   \n\n  "), [])

    def test_短文本只有一个块(self):
        self.assertEqual(chunk_text("你好", size=100, overlap=10), ["你好"])

    def test_长文本切成多块且不超标(self):
        text = "字" * 1200
        chunks = chunk_text(text, size=300, overlap=50)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 302)  # 允许在标点处多 1 个字符

    def test_块之间有重叠(self):
        text = "A" * 400
        chunks = chunk_text(text, size=100, overlap=20)
        self.assertEqual(chunks[0][-20:], chunks[1][:20])

    def test_优先在段落边界切开(self):
        text = "甲" * 60 + "\n\n" + "乙" * 60
        chunks = chunk_text(text, size=100, overlap=10)
        self.assertEqual(chunks[0], "甲" * 60, "应该在段落分隔处断开，而不是硬切")

    def test_优先在句号处切开(self):
        # 规则：切点必须落在窗口后半段，否则会切出太碎的块。
        # 这里句号在第 25 字，窗口 40 字，属于后半段 → 应该采纳。
        text = "甲" * 25 + "。" + "乙" * 40
        chunks = chunk_text(text, size=40, overlap=5)
        self.assertTrue(chunks[0].endswith("。"), f"实际切点：{chunks[0]!r}")

    def test_句子太靠前时不采纳切点(self):
        # 句号在第 6 字，窗口 20 字，属于前半段 → 宁可硬切，也不切出 6 字碎片
        text = "这是第一句。" + "乙" * 80
        chunks = chunk_text(text, size=20, overlap=2)
        self.assertGreater(len(chunks[0]), 10, "不应该在这么早的句号处切")

    def test_参数非法要报错(self):
        with self.assertRaises(ValueError):
            chunk_text("x", size=0)
        with self.assertRaises(ValueError):
            chunk_text("x", size=10, overlap=10)
        with self.assertRaises(ValueError):
            chunk_text("x", size=10, overlap=-1)

    def test_默认参数可用(self):
        self.assertEqual(len(chunk_text("短")), 1)
        self.assertGreater(DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP)


# ---------------------------------------------------------------- HTML 清洗

class TestCleanHtml(unittest.TestCase):
    def test_抽出标题和正文(self):
        html = "<html><head><title>我的文章</title></head><body><p>第一段</p><p>第二段</p></body></html>"
        title, text = clean_html(html)
        self.assertEqual(title, "我的文章")
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertIn("\n", text, "块级标签应该换成换行")

    def test_丢弃脚本和样式(self):
        html = "<body><script>alert('x')</script><style>.a{color:red}</style><p>正文</p></body>"
        _, text = clean_html(html)
        self.assertNotIn("alert", text)
        self.assertNotIn("color", text)
        self.assertIn("正文", text)

    def test_解码HTML实体(self):
        _, text = clean_html("<p>A &amp; B &lt;tag&gt;</p>")
        self.assertIn("A & B <tag>", text)

    def test_畸形HTML不抛异常(self):
        title, text = clean_html("<p>没有闭合的段落<div><span>乱")
        self.assertIsInstance(title, str)
        self.assertIn("没有闭合的段落", text)


# ---------------------------------------------------------------- 抓取

class TestFetchUrl(unittest.TestCase):
    def test_抓取成功(self):
        html = "<title>网页标题</title><p>网页正文</p>"
        with mock.patch("agentlab.ingest.urllib.request.urlopen",
                        return_value=FakeHTTPResponse(html.encode("utf-8"))):
            title, text = fetch_url("https://example.com/a")
        self.assertEqual(title, "网页标题")
        self.assertIn("网页正文", text)

    def test_非http链接被拒(self):
        with self.assertRaises(IngestError):
            fetch_url("ftp://example.com/x")

    def test_HTTP错误转成IngestError(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("agentlab.ingest.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(IngestError):
                fetch_url("https://example.com/missing")

    def test_没有正文时报错(self):
        with mock.patch("agentlab.ingest.urllib.request.urlopen",
                        return_value=FakeHTTPResponse(b"<html><body></body></html>")):
            with self.assertRaises(IngestError):
                fetch_url("https://example.com/empty")


# ---------------------------------------------------------------- 端到端

class TestIngestPipeline(unittest.TestCase):
    def setUp(self):
        self.store = Store(new_temp_dir(self, "ingest") / "kb.db")
        self.pipeline = IngestPipeline(
            self.store,
            HashingEmbedder(dim=32),
            chunk_size=120,
            overlap=20,
        )

    def tearDown(self):
        self.store.close()

    def test_录入文本并入库(self):
        result = self.pipeline.ingest_text("第一段内容。\n\n第二段内容。" * 20, title="测试文档")

        self.assertEqual(result.title, "测试文档")
        self.assertGreater(result.chunk_count, 1)
        self.assertEqual(self.store.stats()["documents"], 1)
        self.assertEqual(self.store.stats()["chunks"], result.chunk_count)

        doc = self.store.get_document(result.document_id)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.title, "测试文档")

    def test_没给标题时用第一行(self):
        result = self.pipeline.ingest_text("这是标题行\n这是正文内容")
        self.assertEqual(result.title, "这是标题行")

    def test_空内容要报错(self):
        with self.assertRaises(IngestError):
            self.pipeline.ingest_text("   ")

    def test_录入URL时记录来源(self):
        with mock.patch("agentlab.ingest.fetch_url", return_value=("网页标题", "网页正文内容")):
            result = self.pipeline.ingest_url("https://example.com/post")

        self.assertEqual(result.source, "https://example.com/post")
        self.assertEqual(result.title, "网页标题")
        self.assertEqual(self.store.stats()["documents"], 1)

    def test_每块都有向量且维度一致(self):
        self.pipeline.ingest_text("内容" * 200, title="向量检查")
        chunks = self.store.all_chunks()
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(len(chunk.embedding), 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
