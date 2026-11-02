"""存储层测试：用临时数据库，跑完自动删。"""

from __future__ import annotations

import unittest
from pathlib import Path

from agentlab.storage import Store
from tests.support import new_temp_dir


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.db_path = new_temp_dir(self, "storage") / "test.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()

    def add(self, title="标题", content="正文", chunks=("块1", "块2"), source=None):
        return self.store.add_document_with_chunks(
            title=title,
            content=content,
            chunks=list(chunks),
            embeddings=[[0.1, 0.2] for _ in chunks],
            source=source,
        )


class TestWriteAndRead(StoreTestCase):
    def test_写入并返回自增ID(self):
        first = self.add(title="第一篇")
        second = self.add(title="第二篇")
        self.assertEqual(first, 1)
        self.assertEqual(second, 2)

    def test_统计数量(self):
        self.add(chunks=("a", "b", "c"))
        stats = self.store.stats()
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(stats["chunks"], 3)

    def test_列表按新到旧(self):
        self.add(title="旧")
        self.add(title="新")
        titles = [doc.title for doc in self.store.list_documents()]
        self.assertEqual(titles, ["新", "旧"])

    def test_列表带片段数(self):
        self.add(chunks=("a", "b", "c"))
        doc = self.store.list_documents()[0]
        self.assertEqual(doc.chunk_count, 3)

    def test_读取单篇(self):
        doc_id = self.add(title="有标题", source="https://example.com")
        doc = self.store.get_document(doc_id)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.title, "有标题")
        self.assertEqual(doc.source, "https://example.com")

    def test_读取不存在返回None(self):
        self.assertIsNone(self.store.get_document(999))

    def test_全部片段带向量和标题(self):
        self.add(title="文章", chunks=("第一段", "第二段"))
        chunks = self.store.all_chunks()
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].title, "文章")
        self.assertEqual(chunks[0].ordinal, 0)
        self.assertEqual(chunks[0].embedding, [0.1, 0.2])


class TestConstraints(StoreTestCase):
    def test_片段数与向量数不一致要报错(self):
        with self.assertRaises(ValueError):
            self.store.add_document_with_chunks(
                title="x", content="x", chunks=["a", "b"], embeddings=[[0.1]]
            )

    def test_没有片段要报错(self):
        with self.assertRaises(ValueError):
            self.store.add_document_with_chunks(
                title="x", content="x", chunks=[], embeddings=[]
            )

    def test_删除文档会级联删片段(self):
        doc_id = self.add(chunks=("a", "b"))
        self.assertTrue(self.store.delete_document(doc_id))
        self.assertEqual(self.store.stats()["chunks"], 0)
        self.assertEqual(self.store.stats()["documents"], 0)

    def test_删除不存在的文档返回False(self):
        self.assertFalse(self.store.delete_document(999))

    def test_数据持久化到文件(self):
        self.add(title="持久化")
        self.store.close()
        reopened = Store(self.db_path)
        try:
            self.assertEqual(reopened.stats()["documents"], 1)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
