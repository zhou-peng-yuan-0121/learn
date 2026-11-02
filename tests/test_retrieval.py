"""混合检索测试：两个单路 + RRF 融合 + 降级行为。"""

from __future__ import annotations

import unittest

from agentlab.embedding import HashingEmbedder
from agentlab.knowledge_tools import format_hits, make_knowledge_tools
from agentlab.retrieval import HybridRetriever, SearchHit, fuse_rrf
from agentlab.storage import Store
from tests.support import new_temp_dir

DOCS = [
    (
        "RAG 学习笔记",
        "RAG 检索增强生成的核心是先把外部知识检索出来再拼进提示词交给模型回答。分块大小是关键参数，块太大检索不精准，块太小会丢上下文。",
    ),
    (
        "向量检索原理",
        "向量检索把文本映射到高维空间，语义相近的文本距离更近，用余弦相似度衡量两个向量的方向一致性。",
    ),
    (
        "午餐记录",
        "今天中午吃了牛肉面，味道不错，花了三十块钱。",
    ),
]


class RetrievalTestCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(new_temp_dir(self, "retrieval") / "kb.db")
        self.embedder = HashingEmbedder(dim=128)
        for title, text in DOCS:
            chunks = [text]
            self.store.add_document_with_chunks(
                title=title,
                content=text,
                chunks=chunks,
                embeddings=self.embedder.embed(chunks),
            )
        self.retriever = HybridRetriever(self.store, self.embedder)

    def tearDown(self):
        self.store.close()


class TestKeywordSearch(RetrievalTestCase):
    def test_精确术语能命中(self):
        hits = self.retriever.search_keyword("RAG", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "RAG 学习笔记")

    def test_短查询降级为LIKE(self):
        # "牛肉" 只有 2 个字，trigram 需要 3 字，必须走 LIKE 兜底
        hits = self.retriever.search_keyword("牛肉", limit=5)
        self.assertTrue(hits, "短查询必须能命中")
        self.assertEqual(hits[0].title, "午餐记录")

    def test_长中文查询能命中(self):
        hits = self.retriever.search_keyword("余弦相似度", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "向量检索原理")

    def test_中文长句无空格也能命中(self):
        # 「余弦相似度怎么算」没有空格，原文里不存在这个精确子串，
        # 靠 2 字滑窗（余弦/弦相/相似/似度…）才可能召回
        hits = self.retriever.search_keyword("余弦相似度怎么算", limit=5)
        self.assertTrue(hits, "中文长句必须能召回")
        self.assertEqual(hits[0].title, "向量检索原理")

    def test_混合模式也能处理中文长句(self):
        hits = self.retriever.search("向量检索怎么工作", top_k=5)
        self.assertTrue(hits)
        titles = [hit.title for hit in hits]
        # 不断言具体排名（受数据影响），只断言两条相关笔记都被召回
        self.assertIn("向量检索原理", titles)
        self.assertIn("RAG 学习笔记", titles)

    def test_没有匹配返回空(self):
        self.assertEqual(self.retriever.search_keyword("量子纠缠", limit=5), [])

    def test_空查询和非法limit(self):
        self.assertEqual(self.retriever.search_keyword("", limit=5), [])
        self.assertEqual(self.retriever.search_keyword("RAG", limit=0), [])

    def test_特殊字符不会让FTS语法崩掉(self):
        for query in ['"未闭合', "a:b", "x-y", "c*", "NEAR("]:
            with self.subTest(query=query):
                self.retriever.search_keyword(query, limit=5)  # 不抛异常即可


class TestVectorSearch(RetrievalTestCase):
    def test_语义相近的排前面(self):
        hits = self.retriever.search_vector("余弦相似度", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "向量检索原理")

    def test_结果带排名(self):
        hits = self.retriever.search_vector("向量", limit=5)
        self.assertEqual(hits[0].vector_rank, 1)

    def test_维度不一致时跳过而不是崩溃(self):
        store = Store(new_temp_dir(self, "dim") / "kb.db")
        try:
            chunks = ["不同维度的旧数据"]
            store.add_document_with_chunks(
                title="旧数据", content=chunks[0], chunks=chunks,
                embeddings=HashingEmbedder(dim=64).embed(chunks),
            )
            retriever = HybridRetriever(store, HashingEmbedder(dim=128))
            self.assertEqual(retriever.search_vector("旧数据", limit=5), [])
        finally:
            store.close()


class TestHybridSearch(RetrievalTestCase):
    def test_混合模式融合两路(self):
        hits = self.retriever.search("向量 相似度", top_k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "向量检索原理")
        # 融合结果应该标注出它来自哪几路
        self.assertIsNotNone(hits[0].keyword_rank)
        self.assertIsNotNone(hits[0].vector_rank)

    def test_只出现在一路里的结果也会保留(self):
        hits = self.retriever.search("牛肉面", top_k=5)
        self.assertTrue(any(hit.title == "午餐记录" for hit in hits))

    def test_单路模式(self):
        keyword_hits = self.retriever.search("检索增强生成", top_k=3, mode="keyword")
        vector_hits = self.retriever.search("检索增强生成", top_k=3, mode="vector")
        self.assertTrue(keyword_hits)
        self.assertTrue(all(hit.vector_rank is None for hit in keyword_hits))
        self.assertTrue(vector_hits, "完整的中文短语应该能算出正向相似度")
        self.assertTrue(all(hit.keyword_rank is None for hit in vector_hits))

    def test_未知模式报错(self):
        with self.assertRaises(ValueError):
            self.retriever.search("x", mode="magic")

    def test_top_k限制(self):
        self.assertLessEqual(len(self.retriever.search("检索", top_k=1)), 1)
        self.assertEqual(self.retriever.search("检索", top_k=0), [])


class TestFuseRrf(unittest.TestCase):
    def hit(self, chunk_id: int, title: str = "t") -> SearchHit:
        return SearchHit(chunk_id=chunk_id, document_id=1, title=title, text="x")

    def test_两路都命中的排第一(self):
        keyword = [self.hit(1), self.hit(2)]   # chunk 2 在关键词排第 2
        vector = [self.hit(2), self.hit(3)]    # chunk 2 在语义排第 1

        fused = fuse_rrf(keyword, vector, k=60)

        self.assertEqual(fused[0].chunk_id, 2)
        self.assertAlmostEqual(fused[0].score, 1 / 62 + 1 / 61)  # 两路都出现 → 分数相加
        self.assertEqual(fused[0].keyword_rank, 2)
        self.assertEqual(fused[0].vector_rank, 1)
        # 只出现在一路的排它后面
        self.assertAlmostEqual(fused[1].score, 1 / 61)

    def test_空输入返回空(self):
        self.assertEqual(fuse_rrf([], []), [])

    def test_融合结果按分数倒序(self):
        fused = fuse_rrf([self.hit(1), self.hit(2)], [self.hit(2), self.hit(3)])
        scores = [hit.score for hit in fused]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestSearchHit(unittest.TestCase):
    def test_摘要会截断(self):
        hit = SearchHit(chunk_id=1, document_id=1, title="t", text="字" * 400)
        self.assertLessEqual(len(hit.snippet), 200)
        self.assertTrue(hit.snippet.endswith("…"))

    def test_解释来源(self):
        hit = SearchHit(chunk_id=1, document_id=1, title="t", text="x",
                        keyword_rank=1, vector_rank=3)
        self.assertIn("关键词第 1", hit.explain())
        self.assertIn("语义第 3", hit.explain())

    def test_序列化(self):
        hit = SearchHit(chunk_id=1, document_id=2, title="标题", text="正文", score=0.1234567)
        data = hit.as_dict()
        self.assertEqual(data["title"], "标题")
        self.assertEqual(data["score"], 0.123457)


class TestKnowledgeTools(RetrievalTestCase):
    def test_工具能检索并返回紧凑文本(self):
        registry = make_knowledge_tools(self.retriever, default_top_k=2)
        self.assertIn("search_knowledge", registry.names())

        output = registry.call("search_knowledge", {"query": "RAG"})
        self.assertIn("RAG 学习笔记", output)
        self.assertIn("相关度", output)

    def test_没结果时给出提示(self):
        registry = make_knowledge_tools(self.retriever)
        output = registry.call("search_knowledge", {"query": "量子纠缠"})
        self.assertIn("没有找到", output)

    def test_空库也给出提示(self):
        store = Store(new_temp_dir(self, "empty") / "kb.db")
        try:
            registry = make_knowledge_tools(HybridRetriever(store, self.embedder))
            output = registry.call("search_knowledge", {"query": "任何东西"})
            self.assertIn("没有找到", output)
        finally:
            store.close()

    def test_top_k被钳制在合理范围(self):
        registry = make_knowledge_tools(self.retriever)
        output = registry.call("search_knowledge", {"query": "检索", "top_k": 999})
        self.assertLessEqual(output.count("相关度"), 10)

    def test_format_hits直接渲染(self):
        hits = self.retriever.search("RAG", top_k=2)
        text = format_hits(hits)
        self.assertIn("找到", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
