"""Embedding 的测试：余弦相似度的数学性质 + 离线嵌入器的确定性。"""

from __future__ import annotations

import io
import json
import math
import unittest
import urllib.error
from unittest import mock

from agentlab.config import Config
from agentlab.embedding import (
    FakeEmbedder,
    HashingEmbedder,
    OpenAIEmbedder,
    cosine_similarity,
    make_embedder,
)


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers = {}

    def read(self, *args) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def make_config(**overrides) -> Config:
    base = dict(base_url="https://api.example.com/v1", api_key="sk-test", model="m")
    base.update(overrides)
    return Config(**base)


class TestCosineSimilarity(unittest.TestCase):
    def test_相同向量是1(self):
        self.assertAlmostEqual(cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0)

    def test_反向向量是负1(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)

    def test_正交向量是0(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_零向量返回0而不是除零崩溃(self):
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_维度不一致要报错(self):
        with self.assertRaises(ValueError):
            cosine_similarity([1, 2], [1, 2, 3])


class TestHashingEmbedder(unittest.TestCase):
    def setUp(self):
        self.embedder = HashingEmbedder(dim=64)

    def test_维度正确且已归一化(self):
        vector = self.embedder.embed(["你好世界"])[0]
        self.assertEqual(len(vector), 64)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0, places=6)

    def test_同一输入结果完全一致(self):
        first = self.embedder.embed(["向量数据库"])[0]
        second = HashingEmbedder(dim=64).embed(["向量数据库"])[0]
        self.assertEqual(first, second, "跨实例必须可复现（不能用内置 hash）")

    def test_相似文本比不相关文本更接近(self):
        base = self.embedder.embed(["向量数据库用于存储和检索高维向量"])[0]
        similar = self.embedder.embed(["向量数据库用来存储和检索向量"])[0]
        unrelated = self.embedder.embed(["今天中午吃了一碗牛肉面"])[0]

        self.assertGreater(
            cosine_similarity(base, similar),
            cosine_similarity(base, unrelated),
        )

    def test_空文本得到零向量(self):
        vector = self.embedder.embed([""])[0]
        self.assertTrue(all(v == 0.0 for v in vector))

    def test_批量返回条数一致(self):
        vectors = self.embedder.embed(["a", "b", "c"])
        self.assertEqual(len(vectors), 3)

    def test_维度太小要报错(self):
        with self.assertRaises(ValueError):
            HashingEmbedder(dim=4)


class TestFakeEmbedder(unittest.TestCase):
    def test_记录调用并保持确定性(self):
        embedder = FakeEmbedder(dim=4)
        first = embedder.embed(["甲"])
        second = embedder.embed(["甲"])
        self.assertEqual(first, second)
        self.assertEqual(embedder.calls, [["甲"], ["甲"]])


class TestOpenAIEmbedder(unittest.TestCase):
    def test_正常解析并更新维度(self):
        embedder = OpenAIEmbedder(make_config(embed_model="text-embedding-3-small"))
        payload = json.dumps({
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ]
        }).encode()

        with mock.patch("agentlab.embedding.urllib.request.urlopen",
                        return_value=FakeHTTPResponse(payload)) as patched:
            vectors = embedder.embed(["第一条", "第二条"])

        self.assertEqual(len(vectors), 2)
        self.assertEqual(embedder.dim, 3)
        request = patched.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/embeddings")
        body = json.loads(request.data.decode())
        self.assertEqual(body["input"], ["第一条", "第二条"])

    def test_HTTP错误转成RuntimeError(self):
        embedder = OpenAIEmbedder(make_config(embed_model="m"))
        error = urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"error":"bad"}'))
        with mock.patch("agentlab.embedding.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                embedder.embed(["x"])

    def test_没有key或模型要报错(self):
        with self.assertRaises(ValueError):
            OpenAIEmbedder(make_config(api_key=""))
        with self.assertRaises(ValueError):
            OpenAIEmbedder(make_config(embed_model=""))


class TestMakeEmbedder(unittest.TestCase):
    def test_没配置模型时用离线实现(self):
        self.assertIsInstance(make_embedder(make_config(embed_model="")), HashingEmbedder)

    def test_配置了模型和key时用远程实现(self):
        embedder = make_embedder(make_config(embed_model="text-embedding-3-small"))
        self.assertIsInstance(embedder, OpenAIEmbedder)


if __name__ == "__main__":
    unittest.main(verbosity=2)
