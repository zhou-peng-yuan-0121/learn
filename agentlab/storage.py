"""存储层：SQLite 里的「文档 + 片段 + 向量 + 全文索引」。

## 为什么用 SQLite

- 单文件、零运维，用户 clone 下来就能用；
- 自带 FTS5 全文索引，第 6 步的「关键词 + 语义」混合检索直接复用；
- 事务保证一致性：一篇文章的正文和它的片段必须同时写成功，
  否则会出现「有文档没片段」的脏数据。

## 表结构

    documents     一篇文档一行：标题、来源、全文、创建时间
    chunks        一个片段一行：属于哪篇、第几段、片段文本、向量（JSON）
    chunks_fts    FTS5 虚拟表，索引 chunks.text（trigram 分词，支持中文子串）

## 为什么用 trigram 分词器

SQLite 默认的 unicode61 会把一整段中文当成**一个 token**，
于是搜「向量」永远匹配不到「向量数据库」这种子串。
trigram 把文本切成 3 字符的滑动窗口，天然支持子串匹配——代价是
**查询词至少要 3 个字符**，所以 Store 里对短查询做了 LIKE 降级。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# trigram 分词器要求查询至少 3 个字符，短于这个长度就走 LIKE
FTS_MIN_QUERY_LEN = 3

_CORE_SCHEMA = """
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

# 外部内容表（content='chunks'）：FTS 表不重复存文本，只存索引，
# 靠触发器与 chunks 表保持同步。
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
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
    score: float = 0.0


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
        self._conn.executescript(_CORE_SCHEMA)
        self._fts_enabled = self._setup_fts()
        self._conn.commit()

    # ---------- 初始化：全文索引 ----------

    def _setup_fts(self) -> bool:
        """建 FTS 表并补齐已有数据；环境不支持 FTS5 时降级为 LIKE 检索。"""
        try:
            self._conn.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 不可用（%s），关键词检索将退化为 LIKE", exc)
            return False

        try:
            total = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            indexed = self._conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            if total != indexed:
                # 老库升级：按 content 表重建索引
                self._conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
                logger.info("已重建全文索引：%d 块", total)
        except sqlite3.OperationalError as exc:
            logger.warning("全文索引同步失败：%s", exc)
            return False
        return True

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

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

    # ---------- 结构化查询 ----------

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
        return [self._row_to_chunk(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        documents = self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {
            "documents": documents,
            "chunks": chunks,
            "path": str(self.path),
            "fts": self._fts_enabled,
        }

    # ---------- 关键词检索 ----------

    @staticmethod
    def _fts_phrase(query: str) -> str:
        """把用户输入包成 FTS5 的「短语」查询，避免 ':' '-' '*' 等被当成语法。"""
        return '"' + query.replace('"', '""') + '"'

    @staticmethod
    def _like_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _is_cjk_run(term: str) -> bool:
        return len(term) > 4 and any("\u4e00" <= char <= "\u9fff" for char in term)

    @classmethod
    def _like_terms(cls, terms: Sequence[str], limit: int = 12) -> list[str]:
        """把查询词展开成适合 LIKE 的子词。

        为什么？——中文没有空格，「向量检索怎么工作」是一整个 term。
        原文里几乎不可能出现这个精确子串，直接 LIKE 会 0 命中。
        所以对长中文词做 2 字滑窗（向量/量检/检索/索怎/怎么/么工/工作），
        任一片段命中即可——粗糙，但对中文检索很有效。
        """
        expanded: list[str] = []
        for term in terms:
            if cls._is_cjk_run(term):
                expanded.extend(term[index:index + 2] for index in range(len(term) - 1))
            else:
                expanded.append(term)

        seen: set[str] = set()
        result: list[str] = []
        for term in expanded:
            if term not in seen:
                seen.add(term)
                result.append(term)
        return result[:limit]

    def search_keyword(self, query: str, limit: int = 10) -> list[Chunk]:
        """关键词检索：FTS 找精确命中，LIKE 兜底，两者合并去重。

        - **FTS 路径**：trigram 索引 + bm25 排序，快且带相关度，
          但要求查询词 >= 3 字符且原文包含该子串；
        - **LIKE 路径**：子串匹配，能覆盖 1~2 字的中文词，
          长中文词会被拆成 2 字滑窗（见 :meth:`_like_terms`）。

        两条都跑、合并结果，是为了不漏召回——FTS 结果排前面（更精准）。
        """
        query = (query or "").strip()
        if not query or limit <= 0:
            return []

        terms = [term for term in query.split() if term]
        if not terms:
            return []

        hits: list[Chunk] = []
        seen_ids: set[int] = set()

        def extend(chunks: Sequence[Chunk]) -> None:
            for chunk in chunks:
                if chunk.id not in seen_ids:
                    seen_ids.add(chunk.id)
                    hits.append(chunk)

        # --- 路径一：FTS（trigram + bm25）---
        fts_terms = [term for term in terms if len(term) >= FTS_MIN_QUERY_LEN]
        if self._fts_enabled and fts_terms:
            expression = " OR ".join(self._fts_phrase(term) for term in fts_terms)
            try:
                rows = self._conn.execute(
                    """
                    SELECT c.*, d.title AS title, bm25(chunks_fts) AS score
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    JOIN documents d ON d.id = c.document_id
                    WHERE chunks_fts MATCH ?
                    ORDER BY score
                    LIMIT ?
                    """,
                    (expression, limit),
                ).fetchall()
                extend([self._row_to_chunk(row) for row in rows])
            except sqlite3.OperationalError as exc:
                logger.debug("FTS 查询失败（%s），仅使用 LIKE", exc)

        # --- 路径二：LIKE 兜底 ---
        if len(hits) < limit:
            like_terms = self._like_terms(terms)
            if like_terms:
                clauses = " OR ".join("c.text LIKE ? ESCAPE '\\'" for _ in like_terms)
                params: list[Any] = [f"%{self._like_escape(term)}%" for term in like_terms]
                params.append(limit)
                rows = self._conn.execute(
                    f"""
                    SELECT c.*, d.title AS title
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE {clauses}
                    ORDER BY c.id
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                extend([self._row_to_chunk(row) for row in rows])

        return hits[:limit]

    # ---------- 内部工具 ----------

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> Chunk:
        keys = row.keys()
        return Chunk(
            id=row["id"],
            document_id=row["document_id"],
            ordinal=row["ordinal"],
            text=row["text"],
            embedding=json.loads(row["embedding"]),
            title=row["title"] if "title" in keys else "",
            score=float(row["score"]) if "score" in keys else 0.0,
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
