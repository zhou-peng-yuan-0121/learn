"""把知识库检索包装成 Agent 工具。

关键点：工具的返回值要塞进模型上下文，所以必须**紧凑**。
返回整篇文档会让 token 爆炸，只给「标题 + 相关度 + 摘录」就够模型判断了。
"""

from __future__ import annotations

from .retrieval import HybridRetriever, SearchHit
from .tools import ToolRegistry, make_spec

DEFAULT_TOP_K = 3


def format_hits(hits: list[SearchHit]) -> str:
    """把检索结果渲染成给模型看的紧凑文本。"""
    if not hits:
        return "知识库里没有找到相关内容。（可能还没录入，或换个说法试试）"

    lines = [f"在知识库中找到 {len(hits)} 条相关片段："]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"[{index}]《{hit.title}》相关度 {hit.score:.4f}")
        lines.append(f"    {hit.snippet}")
    return "\n".join(lines)


def make_knowledge_tools(
    retriever: HybridRetriever,
    *,
    default_top_k: int = DEFAULT_TOP_K,
) -> ToolRegistry:
    """生成知识库相关工具，挂到 Agent 的工具表上。"""

    def search_knowledge(query: str, top_k: int | None = None) -> str:
        """在本人知识库中检索相关笔记片段（query 用自然语言描述要找什么）"""
        limit = default_top_k if top_k is None else int(top_k)
        limit = max(1, min(limit, 10))  # 防止模型要 1000 条把上下文撑爆
        hits = retriever.search(query, top_k=limit)
        return format_hits(hits)

    registry = ToolRegistry()
    registry.add(
        make_spec(
            search_knowledge,
            name="search_knowledge",
            description="检索本人知识库里与问题相关的笔记片段，回答个人化问题前应先调用",
        )
    )
    return registry
