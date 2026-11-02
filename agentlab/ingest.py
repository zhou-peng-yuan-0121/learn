"""知识摄取管线：文本/网页 → 清洗 → 分块 → 向量 → 入库。

## 三个关键决策

**1. 为什么不用 BeautifulSoup？**
零依赖是项目底线，而 ``html.parser`` 是标准库自带的。抽正文只需要
「丢掉 script/style、块级标签换行、解码实体」这三件事，自己写 80 行够了。

**2. 为什么要分块？**
- 模型的上下文窗口有限，整篇塞进去可能超限；
- 检索要精确到「哪一段」，整篇作为一个单位太粗（一问细节就失焦）；
- 块越小，向量越聚焦，语义检索越准；但太小会丢上下文。
所以 500 字左右 + 80 字重叠是个常用折中。

**3. 为什么块之间有重叠？**
句子被从中间切断会让语义丢失。前后各留一点重叠，能保证关键句至少
完整地出现在某一个块里（代价是存储和计算略增）。
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Sequence

from .config import Config
from .embedding import Embedder
from .storage import Store

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80


class IngestError(RuntimeError):
    """摄取失败：抓取失败、内容为空、无法解析等。"""


# ---------------------------------------------------------------- 分块

_WHITESPACE_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """统一换行、压缩空白，为分块做准备。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """把长文本切成有重叠的块。

    切点优先级：段落分隔 > 句末标点 > 硬切。优先在自然边界断开，
    避免把一句话劈成两半。
    """
    if size <= 0:
        raise ValueError("size 必须为正数")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须满足 0 <= overlap < size")

    text = normalize_text(text)
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + size, length)

        if end < length:
            window = text[start:end]
            # 先找段落分隔（后半段里），再退而求其次找句末标点
            cut = window.rfind("\n\n")
            if cut < size // 2:
                candidates = (window.rfind("。"), window.rfind("！"), window.rfind("？"),
                              window.rfind(". "), window.rfind("\n"))
                cut = max(candidates)
            if cut >= size // 2:
                end = start + cut + 1

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= length:
            break
        # 回退 overlap 个字符产生重叠；至少前进 1 个字符，防止死循环
        start = max(end - overlap, start + 1)

    return chunks


# ---------------------------------------------------------------- HTML 清洗

class _TextExtractor(HTMLParser):
    """把 HTML 抽成纯文本，顺便抓 ``<title>``。"""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
    _BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "td", "th", "section", "article",
        "header", "footer", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    }
    _SKIP_SECTION_TAGS = {"nav", "aside", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP_TAGS or tag in self._SKIP_SECTION_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS or tag in self._SKIP_SECTION_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._skip_depth == 0:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return normalize_text("".join(self._title_parts))

    @property
    def text(self) -> str:
        return normalize_text("".join(self._parts))


_TAG_RE = re.compile(r"<[^>]+>")


def clean_html(html: str) -> tuple[str, str]:
    """返回 ``(标题, 正文)``。解析失败时退化成「粗暴去标签」。"""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
        return extractor.title, extractor.text
    except Exception as exc:  # 畸形 HTML 不该让整个摄取失败
        logger.warning("HTML 解析失败（%s），退化为去标签处理", exc)
        text = normalize_text(_TAG_RE.sub(" ", html))
        return "", text


# ---------------------------------------------------------------- 抓取

_HTTP_TIMEOUT = 20.0
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB，防止误抓大文件把内存吃光


def fetch_url(url: str) -> tuple[str, str]:
    """抓一个网页，返回 ``(标题, 正文)``。"""
    if not url.startswith(("http://", "https://")):
        raise IngestError("只支持 http/https 链接")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; agentlab/0.1)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            raw = response.read(_MAX_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise IngestError(f"抓取失败 HTTP {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IngestError(f"抓取失败：{exc}") from exc

    if len(raw) > _MAX_BYTES:
        raw = raw[:_MAX_BYTES]
        logger.warning("页面超过 5MB，已截断")

    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:  # 服务端返回了不认识的字符集名
        html = raw.decode("utf-8", errors="replace")

    title, text = clean_html(html)
    if not text.strip():
        raise IngestError("页面没有提取到正文（可能需要 JS 渲染）")
    return title, text


# ---------------------------------------------------------------- 管线

@dataclass
class IngestResult:
    """一次摄取的结果。"""

    document_id: int
    title: str
    source: str | None
    chunk_count: int
    char_count: int

    def summary(self) -> str:
        return (
            f"已入库 #{self.document_id}《{self.title}》"
            f"（{self.char_count} 字 → {self.chunk_count} 块）"
        )


def _fallback_title(text: str, limit: int = 40) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    first_line = first_line.strip()
    if len(first_line) > limit:
        first_line = first_line[:limit] + "…"
    return first_line or "无标题"


class IngestPipeline:
    """把「一段文本」变成「库里可检索的文档」。"""

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.chunk_size = chunk_size
        self.overlap = overlap

    def ingest_text(
        self,
        text: str,
        *,
        title: str | None = None,
        source: str | None = None,
    ) -> IngestResult:
        """录入一段纯文本。"""
        if not isinstance(text, str) or not text.strip():
            raise IngestError("内容为空，没什么可入库的")

        cleaned = normalize_text(text)
        chunks = chunk_text(cleaned, size=self.chunk_size, overlap=self.overlap)
        if not chunks:
            raise IngestError("分块后为空")

        embeddings = self.embedder.embed(chunks)
        document_id = self.store.add_document_with_chunks(
            title=(title or _fallback_title(cleaned)).strip(),
            content=cleaned,
            chunks=chunks,
            embeddings=embeddings,
            source=source,
        )
        logger.info("入库 #%d：%d 字 → %d 块", document_id, len(cleaned), len(chunks))
        return IngestResult(
            document_id=document_id,
            title=(title or _fallback_title(cleaned)).strip(),
            source=source,
            chunk_count=len(chunks),
            char_count=len(cleaned),
        )

    def ingest_url(self, url: str) -> IngestResult:
        """抓一个网页并录入。"""
        title, text = fetch_url(url)
        return self.ingest_text(text, title=title or None, source=url)


def default_store(config: Config) -> Store:
    """按配置打开默认数据库（``data/knowledge.db``）。"""
    return Store(config.data_dir() / "knowledge.db")


def default_pipeline(config: Config, store: Store | None = None) -> IngestPipeline:
    """组装一条默认可用的摄取管线。"""
    from .embedding import make_embedder

    return IngestPipeline(store or default_store(config), make_embedder(config))
