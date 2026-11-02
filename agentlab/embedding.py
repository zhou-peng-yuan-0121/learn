"""Embedding：把文本变成向量。

## 向量到底是什么

一段文本 → 一个固定长度的浮点数组，比如 512 维。语义越接近的文本，
它们在这个高维空间里的**方向**越接近；用余弦相似度衡量（0 = 无关，1 = 完全一致）。

为什么能这样？因为训练时模型被要求「把出现在相似上下文里的词映射到相近位置」。
所以我们能问「语义上最像的段落是哪段」，而不只是「哪个关键词出现了」。

## 为什么要有两个实现

- :class:`OpenAIEmbedder` —— 真实语义向量，效果好，但要 key、要钱、要联网；
- :class:`HashingEmbedder` —— 离线、确定性、零依赖。

HashingEmbedder 不是玩具：它把字符 n-gram 哈希到固定维度，本质是「词面相似度」。
它能区分「向量数据库」和「今天天气」，但分不清「吃饭」和「用餐」（同义词它不懂）。
好处是整个摄取/检索管线可以脱网跑测试，不用为验证逻辑花钱。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request
from typing import Protocol, Sequence

from .config import Config

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    """嵌入器协议：给一批文本，返回一批等长向量。"""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度 = 点积 / (模长乘积)。

    为什么不用欧氏距离？——向量长度可能不同（长文本天然模长更大），
    我们关心的是「方向像不像」，不是「长度像不像」，所以归一化后用余弦。
    """
    if len(a) != len(b):
        raise ValueError(f"向量维度不一致：{len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class HashingEmbedder:
    """离线确定性嵌入器：字符/词 n-gram → 哈希桶 → 归一化向量。

    - 同样的输入永远得到同样的向量（用 blake2b，不用 Python 内置 hash，
      因为内置 hash 每个进程启动时都加随机盐，不可复现）；
    - 随机符号（+1/-1）用来抵消哈希碰撞带来的系统性偏高；
    - 输出 L2 归一化，于是相似度直接就是点积。
    """

    def __init__(self, dim: int = 512, ngram: int = 2) -> None:
        if dim < 8:
            raise ValueError("dim 至少 8")
        self.dim = dim
        self.ngram = max(1, ngram)

    # ---------- 分词：中文按字 + 双字，英文按词 ----------

    def _tokens(self, text: str) -> list[str]:
        tokens: list[str] = []
        for word in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", text.lower()):
            if not word:
                continue
            if word.isascii():
                tokens.append(word)
            else:
                tokens.extend(word)  # 单字
                tokens.extend(word[i:i + 2] for i in range(len(word) - 1))  # 双字
        if self.ngram > 2:
            grams = ["".join(tokens[i:i + self.ngram]) for i in range(len(tokens) - self.ngram + 1)]
            tokens.extend(grams)
        return tokens

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.dim
            sign = 1.0 if (number >> 63) & 1 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAIEmbedder:
    """调用 OpenAI 兼容的 ``POST /embeddings`` 接口。

    注意：DeepSeek 目前没有 embedding 接口，所以默认不用它；
    想用真实语义向量时，把 base_url 指向支持 embeddings 的服务
    （OpenAI / 智谱 / 硅基流动 / 本地 vLLM 等）并设置 AGENTLAB_EMBED_MODEL。
    """

    def __init__(self, config: Config, *, model: str | None = None) -> None:
        if not config.api_key:
            raise ValueError("使用 OpenAIEmbedder 需要配置 API key")
        self.config = config
        self.model = model or config.embed_model
        if not self.model:
            raise ValueError("没有指定 embedding 模型（AGENTLAB_EMBED_MODEL）")
        self.dim = 0  # 首次调用后才知道真实维度

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self.config.base_url}/embeddings"
        body = json.dumps({"model": self.model, "input": list(texts)}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "User-Agent": "agentlab/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"embedding 调用失败 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"embedding 网络错误: {exc}") from exc

        data = payload.get("data") or []
        if len(data) != len(texts):
            raise RuntimeError(f"返回向量条数不符：期望 {len(texts)}，收到 {len(data)}")
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
        if vectors:
            self.dim = len(vectors[0])
        return vectors


class FakeEmbedder:
    """测试用：把文本映射成可预测的向量（按字符和固定维度）。"""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            seed = sum(ord(char) for char in text) or 1
            vector = [float((seed * (index + 1)) % 97) for index in range(self.dim)]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def make_embedder(config: Config) -> Embedder:
    """按配置选嵌入器：有模型名 + key 就走真实接口，否则用离线的。"""
    if config.embed_model and config.api_key:
        try:
            return OpenAIEmbedder(config)
        except ValueError as exc:
            logger.warning("无法使用远程 embedding（%s），回退到离线实现", exc)
    return HashingEmbedder()
