"""LLM 层：模型调用的唯一入口。

这一层只干三件事：

1. **屏蔽厂商差异**：主流模型基本都实现了 OpenAI 的 ``POST /chat/completions``
   协议（DeepSeek、Moonshot、通义、vLLM/Ollama 的兼容模式）。上层代码只认
   :class:`ChatModel` 协议，换模型改一个 URL 就行。
2. **支持流式输出**：非流式要等整段生成完（可能 20 秒），流式一个字一个字
   吐（SSE），前端才能做「打字机」效果，也让人感觉快。
3. **提供假模型**：:class:`FakeLLM` 让 Agent/RAG 逻辑可以在没有 key、
   没有网络的机器上被测试——这是能不能持续重构的关键。

关于「为什么不用 openai 官方 SDK」：SDK 也就多一层封装，而我们只需要
一个 POST + 一份 SSE 解析。用标准库 ``urllib`` 换来的是零依赖、可读、
没有黑盒——学习阶段这一点比省 20 行代码重要得多。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol, Sequence

from .config import Config

logger = logging.getLogger(__name__)

# 一条消息就是 {"role": "system"|"user"|"assistant"|"tool", "content": "..."}
Message = dict[str, Any]


class LLMError(RuntimeError):
    """模型调用失败：没有 key、网络错误、HTTP 错误、返回体不合法。"""


@dataclass
class Usage:
    """token 用量统计。

    为什么非要有这个类？—— 因为「Agent 跑一次花多少钱」是工程问题，
    不是学术问题。不记账，你就不知道自己的循环是不是在烧钱。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merge(self, other: "Usage") -> "Usage":
        """把另一次调用的用量累加进自身，返回 self 方便链式调用。"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        return self

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Usage":
        """从 API 返回的 usage 字段构造（字段缺失时按 0 处理）。"""
        if not payload:
            return cls()
        return cls(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMResponse:
    """一次非流式调用的结果。"""

    text: str
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    """上层只依赖这个协议，不依赖任何具体客户端。

    ``Protocol`` 是「结构化类型」：不要求继承，只要方法签名对得上就算实现。
    好处是测试里塞个 duck-typed 的假对象也能跑，不需要为了测试去继承。
    """

    name: str

    def chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        """一次性拿到完整回复。"""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> Iterator[str]:
        """流式拿到增量文本（每段之间是拼起来的关系，不是覆盖）。"""
        ...


def parse_completion(raw_bytes: bytes) -> LLMResponse:
    """把 ``/chat/completions`` 的响应体解析成 :class:`LLMResponse`。"""
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMError(f"返回体不是合法 JSON: {exc}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"响应里没有 choices: {data}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if not isinstance(text, str):
        raise LLMError(f"响应里没有文本内容: {message}")
    return LLMResponse(text=text, usage=Usage.from_payload(data.get("usage")), raw=data)


class OpenAICompatLLM:
    """OpenAI ``/chat/completions`` 兼容客户端。"""

    def __init__(self, config: Config, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        if not config.api_key:
            raise LLMError(
                "没有配置 API key。请把 .env.example 复制成 .env 并填 AGENTLAB_API_KEY；"
                "如果你想先不连网跑通流程，用 demo-llm 命令里的 FakeLLM。"
            )
        self.config = config
        self.name = config.model
        # 注入 sleeper 是为了测试重试逻辑时不真的等 0.8 秒
        self._sleeper = sleeper

    # ---------- 内部：请求与重试 ----------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "agentlab/0.1",
        }

    def _payload(
        self,
        messages: Sequence[Message],
        temperature: float | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": stream,
        }

    def _post(self, payload: dict[str, Any]) -> Any:
        """发请求并返回响应对象（自带重试）。

        重试策略：只对「可能自己会好」的错误重试——网络抖动、429 限流、5xx。
        像 401（key 错）和 400（参数错）重试一万次也没用，立刻抛错，
        免得把用户的额度耗在无意义的重试上。
        """
        url = f"{self.config.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: LLMError | None = None

        for attempt in range(self.config.max_retries + 1):
            request = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
            try:
                return urllib.request.urlopen(request, timeout=self.config.request_timeout)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last_error = LLMError(f"HTTP {exc.code}: {detail}")
                if exc.code < 500 and exc.code != 429:
                    raise last_error from exc
                logger.warning("LLM 返回 %s，第 %d 次重试", exc.code, attempt + 1)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = LLMError(f"网络错误: {exc}")
                logger.warning("LLM 网络异常（%s），第 %d 次重试", exc, attempt + 1)

            if attempt < self.config.max_retries:
                # 线性退避：0.8s, 1.6s, 2.4s…… 简单够用
                self._sleeper(0.8 * (attempt + 1))

        raise last_error or LLMError("未知的调用失败")

    # ---------- 对外：两种调用方式 ----------

    def chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload = self._payload(messages, temperature, stream=False)
        response = self._post(payload)
        with response:
            raw = response.read()
        return parse_completion(raw)

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> Iterator[str]:
        """逐段产出文本增量。

        流的格式是 SSE：每一行 ``data: {json}``，最后一行 ``data: [DONE]``。
        增量在 ``choices[0].delta.content`` 里。
        """
        payload = self._payload(messages, temperature, stream=True)
        response = self._post(payload)

        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue  # 空行是 SSE 分隔符；以 ':' 开头的是注释行
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("跳过无法解析的 SSE 片段: %r", line)
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield text


class FakeLLM:
    """脚本化的假模型。

    用途：
    - **测试**：不用花钱、不用网络，2 秒跑完全部用例；
    - **演示**：别人 clone 你的仓库，没有 API key 也能看到完整效果；
    - **回归**：真实模型行为不可复现，但你的 Agent 逻辑必须可复现。

    ``script`` 可以是字符串列表（每次 chat 弹出一条，用完重复最后一条），
    也可以是一个函数 ``Callable[[messages], str]``，做完全自定义的桩。
    """

    def __init__(
        self,
        script: Sequence[str | LLMResponse] | Callable[[Sequence[Message]], str | LLMResponse],
        *,
        name: str = "fake-llm",
        chunk_size: int = 4,
    ) -> None:
        self._script = script
        self._cursor = 0
        self._chunk_size = max(1, chunk_size)
        self.name = name
        # 记录每次收到的消息，方便断言「Agent 到底把什么喂给了模型」
        self.calls: list[list[Message]] = []

    def _next(self, messages: Sequence[Message]) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])

        if callable(self._script):
            result = self._script(messages)
        else:
            if not self._script:
                raise LLMError("FakeLLM 没有可用脚本")
            index = min(self._cursor, len(self._script) - 1)
            self._cursor += 1
            result = self._script[index]

        if isinstance(result, LLMResponse):
            return result
        return LLMResponse(text=result, usage=Usage(prompt_tokens=8, completion_tokens=8))

    def chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> LLMResponse:
        return self._next(messages)

    def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
    ) -> Iterator[str]:
        """把整段回复切成小块吐出来，模拟流式。"""
        text = self._next(messages).text
        for start in range(0, len(text), self._chunk_size):
            yield text[start:start + self._chunk_size]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> list[Message]:
        return self.calls[-1] if self.calls else []


def make_llm(config: Config) -> ChatModel:
    """按配置创建真实模型客户端。

    统一的构造入口——以后加「本地 Ollama」「Anthropic 适配」都从这里分发，
    上层永远只 ``make_llm(config)``。
    """
    return OpenAICompatLLM(config)
