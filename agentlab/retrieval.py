"""混合检索：关键词 + 语义，再用 RRF 融合。

## 为什么需要两路

| 查询 | 关键词检索 | 语义检索 |
|---|---|---|
| `FTS5`（精确术语） | ✅ 精确命中 | 一般，向量对短专有名词不敏感 |
| `怎么让模型记住我的笔记` | ❌ 逐字匹配不到 | ✅ 能找到 RAG 相关段落 |
| `向量`（2 个字） | FTS 的 trigram 需要 3 字，退化 LIKE | ✅ |

两路各自有盲区，合起来才互补。

## 为什么用 RRF 融合

两路的分数不可比：bm25 是负数（越小越好），余弦在 -1~1 之间。
直接加权相加需要调参数，而且每次换嵌入模型都要重调。

**RRF（Reciprocal Rank Fusion）只看排名，不看分数**：

    score(d) = Σ 1 / (k + rank_i(d))

k 通常取 60。同时在两路里排第一的文档得 2/(61)，只在某一路排第一的得 1/61。
简单、无需调参、对分数尺度不敏感——这是工业界很常用的做法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from .embedding import Embedder, cosine_similarity
from .storage import Chunk, Store

logger = logging.getLogger(__name__)

DEFAULT_RRF_K = 60
SNIPPET_LENGTH = 180

# 相似度下限：低于这个值的「命中」基本都是噪声。
# 为什么需要它？——余弦相似度很少真的是 0。哈希向量对完全无关的文本也会
# 因为哈希碰撞给出 0.01~0.05 的分数，直接返回会把不相关内容塞给模型。
# 真实语义模型的无关相似度通常在 0.1~0.3，所以换模型时要相应调高。
DEFAULT_MIN_SIMILARITY = 0.05


@dataclass
class SearchHit:
    """一条检索结果。"""

    chunk_id: int
    document_id: int
    title: str
    text: str
    score: float = 0.0
    keyword_rank: int | None = None
    vector_rank: int | None = None

    @property
    def snippet(self) -> str:
        text = " ".join(self.text.split())
        if len(text) <= SNIPPET_LENGTH:
            return text
        return text[:SNIPPET_LENGTH] + "…"

    def explain(self) -> str:
        """说明这条结果是怎么被找到的——调试检索问题时非常有用。"""
        parts = []
        if self.keyword_rank is not None:
            parts.append(f"关键词第 {self.keyword_rank}")
        if self.vector_rank is not None:
            parts.append(f"语义第 {self.vector_rank}")
        return " + ".join(parts) if parts else "未知来源"

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "text": self.text,
            "snippet": self.snippet,
            "score": round(self.score, 6),
            "keyword_rank": self.keyword_rank,
            "vector_rank": self.vector_rank,
        }


def _hit_from_chunk(chunk: Chunk) -> SearchHit:
    return SearchHit(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        title=chunk.title,
        text=chunk.text,
    )


def fuse_rrf(
    keyword_hits: Iterable[SearchHit],
    vector_hits: Iterable[SearchHit],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[SearchHit]:
    """倒数排名融合。

    同一个 chunk 在两路里都出现时分数相加，排名自然上升。
    """
    merged: dict[int, SearchHit] = {}

    def add(hits: Iterable[SearchHit], rank_attr: str) -> None:
        for rank, hit in enumerate(hits, start=1):
            entry = merged.get(hit.chunk_id)
            if entry is None:
                entry = _hit_from_chunk(
                    Chunk(
                        id=hit.chunk_id,
                        document_id=hit.document_id,
                        ordinal=0,
                        text=hit.text,
                        embedding=[],
                        title=hit.title,
                    )
                )
                merged[hit.chunk_id] = entry
            setattr(entry, rank_attr, rank)
            entry.score += 1.0 / (k + rank)

    add(keyword_hits, "keyword_rank")
    add(vector_hits, "vector_rank")

    return sorted(merged.values(), key=lambda item: item.score, reverse=True)


class HybridRetriever:
    """把 SQLite 关键词检索和向量余弦检索组合起来。"""

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        *,
        rrf_k: int = DEFAULT_RRF_K,
        min_similarity: float | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.rrf_k = rrf_k
        # 下限优先取嵌入器自己声明的（它知道自己噪声多大），否则用默认值
        if min_similarity is None:
            min_similarity = float(
                getattr(embedder, "suggested_min_similarity", DEFAULT_MIN_SIMILARITY)
            )
        self.min_similarity = min_similarity

    # ---------- 单路检索 ----------

    def search_keyword(self, query: str, limit: int = 10) -> list[SearchHit]:
        return [_hit_from_chunk(chunk) for chunk in self.store.search_keyword(query, limit)]

    def search_vector(self, query: str, limit: int = 10) -> list[SearchHit]:
        """暴力遍历全部片段算余弦相似度。

        为什么不建向量索引？——数据量小（几千条以内）时暴力遍历是毫秒级，
        引入 HNSW 之类的索引会让依赖和复杂度暴增。等真的慢了再换，是更理性的顺序。
        """
        query = (query or "").strip()
        if not query or limit <= 0:
            return []

        query_vector = self.embedder.embed([query])[0]
        scored: list[tuple[float, Chunk]] = []
        for chunk in self.store.all_chunks():
            if len(chunk.embedding) != len(query_vector):
                # 换过嵌入模型时维度会变；跳过旧数据，而不是整个查询崩掉
                continue
            score = cosine_similarity(query_vector, chunk.embedding)
            if score >= self.min_similarity:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[SearchHit] = []
        for rank, (score, chunk) in enumerate(scored[:limit], start=1):
            hit = _hit_from_chunk(chunk)
            hit.score = score
            hit.vector_rank = rank
            hits.append(hit)
        return hits

    # ---------- 混合检索 ----------

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        """主入口。``mode`` 可选 ``hybrid`` / ``keyword`` / ``vector``。"""
        if top_k <= 0:
            return []
        if mode == "keyword":
            return self.search_keyword(query, top_k)
        if mode == "vector":
            return self.search_vector(query, top_k)
        if mode != "hybrid":
            raise ValueError(f"未知检索模式：{mode!r}（可选 hybrid/keyword/vector）")

        # 两路各取多一些候选，融合后再截断，避免某一路的高分项被过早淘汰
        candidate_limit = max(top_k * 3, top_k + 4)
        keyword_hits = self.search_keyword(query, candidate_limit)
        vector_hits = self.search_vector(query, candidate_limit)
        fused = fuse_rrf(keyword_hits, vector_hits, k=self.rrf_k)
        return fused[:top_k]
