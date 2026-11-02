"""存储层：SQLite 里的「文档 + 片段 + 向量」。

## 为什么用 SQLite

- 单文件、零运维，用户 clone 下来就能用；
- 自带全文检索（FTS5），第 6 步的「关键词 + 语义」混合检索直接复用；
- 事务保证一致性：一篇文章的正文和它的片段必须同时写成功，
  否则会出现「有文档没片段」的脏数据。

## 表结构

    documents  一篇文档一行：标题、来源、全文、创建时间
    chunks     一个片段一行：属于哪篇、第几段、片段文本、向量（JSON）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    source     TEXT,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
"""


@dataclass
class Document:
    """一篇已入库的文档。"""

    id: int
    title: str
    source: str | None
    content: str
    created_at: str
    chunk_count: int = 0


@dataclass
class Chunk:
    """一个片段及其向量。"""

    id: int
    document_id: int
    ordinal: int
    text: str
    embedding: list[float]
    title: str = ""


class Store:
    """SQLite 存储门面。

    所有 SQL 都收在这个类里——上层（摄取管线、检索、Web）不碰 SQL，
    将来换 PostgreSQL 只改这一个文件。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------- 写入 ----------

    def add_document_with_chunks(
        self,
        *,
        title: str,
        content: str,
        chunks: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        source: str | None = None,
    ) -> int:
        """一篇文档 + 它的全部片段，在一个事务里写入。

        为什么必须原子？——如果先写文档、写片段时崩了，这篇文档就永远
        「没有片段」，检索永远搜不到它，而且看起来一切正常。
        """
        if len(chunks) != len(embeddings):
            raise ValueError(f"片段数 {len(chunks)} 与向量数 {len(embeddings)} 不一致")
        if not chunks:
            raise ValueError("文档至少要有一个片段")

        created_at = datetime.now().isoformat(timespec="seconds")
        with self._conn:  # 上下文管理器 = 事务：异常时自动回滚
            cursor = self._conn.execute(
                "INSERT INTO documents (title, source, content, created_at) VALUES (?, ?, ?, ?)",
                (title, source, content, created_at),
            )
            document_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO chunks (document_id, ordinal, text, embedding) VALUES (?, ?, ?, ?)",
                [
                    (document_id, ordinal, text, json.dumps(list(vector)))
                    for ordinal, (text, vector) in enumerate(zip(chunks, embeddings))
                ],
            )
        return document_id

    def delete_document(self, document_id: int) -> bool:
        """删除文档及其片段（靠外键级联）。"""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return cursor.rowcount > 0

    # ---------- 查询 ----------

    def list_documents(self, limit: int = 50) -> list[Document]:
        rows = self._conn.execute(
            """
            SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) AS chunk_count
            FROM documents d
            ORDER BY d.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            Document(
                id=row["id"],
                title=row["title"],
                source=row["source"],
                content=row["content"],
                created_at=row["created_at"],
                chunk_count=row["chunk_count"],
            )
            for row in rows
        ]

    def get_document(self, document_id: int) -> Document | None:
        row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return None
        return Document(
            id=row["id"], title=row["title"], source=row["source"],
            content=row["content"], created_at=row["created_at"],
        )

    def all_chunks(self) -> list[Chunk]:
        """取出全部片段——向量检索要遍历（数据量小的时候这样最简单）。"""
        rows = self._conn.execute(
            """
            SELECT c.*, d.title AS title
            FROM chunks c JOIN documents d ON d.id = c.document_id
            ORDER BY c.document_id, c.ordinal
            """
        ).fetchall()
        return [
            Chunk(
                id=row["id"], document_id=row["document_id"], ordinal=row["ordinal"],
                text=row["text"], embedding=json.loads(row["embedding"]), title=row["title"],
            )
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        documents = self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {"documents": documents, "chunks": chunks, "path": str(self.path)}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
