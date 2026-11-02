"""LLM 层的测试 —— 全部脱网、不花一分钱。

关键技巧：用 ``unittest.mock.patch`` 替换 ``urllib.request.urlopen``，
于是「真实客户端」的解析/重试/流式逻辑都能被测到，而完全不碰网络。
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from agentlab.config import Config
from agentlab.llm import (
    FakeLLM,
    LLMError,
    LLMResponse,
    OpenAICompatLLM,
    Usage,
    parse_completion,
)


def make_config(**overrides) -> Config:
    base = dict(
        base_url="https://api.example.com/v1",
        api_key="sk-test-key",
        model="test-model",
    )
    base.update(overrides)
    return Config(**base)


class FakeHTTPResponse:
    """冒充 urlopen 返回的对象：可 read()、可逐行迭代、支持 with。"""

    def __init__(self, payload: bytes = b"", lines: list[bytes] | None = None) -> None:
        self._payload = payload
        self._lines = lines or []
        self.headers = {}

    def read(self, *args, **kwargs) -> bytes:
        return self._payload

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def completion_body(text: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def sse_lines(*chunks: str) -> list[bytes]:
    """构造一段 SSE 响应体：每个 chunk 一行 data:，最后 [DONE]。"""
    lines: list[bytes] = []
    for text in chunks:
        payload = {"choices": [{"index": 0, "delta": {"content": text}}]}
        lines.append(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
    lines.append(b"data: [DONE]\n")
    return lines


class TestParseCompletion(unittest.TestCase):
    def test_解析文本和用量(self):
        response = parse_completion(completion_body("你好", 12, 3))
        self.assertEqual(response.text, "你好")
        self.assertEqual(response.usage.prompt_tokens, 12)
        self.assertEqual(response.usage.completion_tokens, 3)
        self.assertEqual(response.usage.total_tokens, 15)

    def test_没有_choices_要报错(self):
        with self.assertRaises(LLMError):
            parse_completion(b'{"choices": []}')

    def test_不是_JSON_要报错(self):
        with self.assertRaises(LLMError):
            parse_completion(b"<html>502 Bad Gateway</html>")


class TestOpenAICompatChat(unittest.TestCase):
    def test_正常返回(self):
        llm = OpenAICompatLLM(make_config())
        with mock.patch(
            "agentlab.llm.urllib.request.urlopen",
            return_value=FakeHTTPResponse(completion_body("你好")),
        ) as patched:
            response = llm.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.text, "你好")
        # 验证我们发出去的是正确的请求：URL、鉴权头、body 里的 model
        request = patched.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])

    def test_500_会重试_第二次成功(self):
        error = urllib.error.HTTPError(
            "https://api.example.com/v1/chat/completions",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"error": "boom"}'),
        )
        sleeps: list[float] = []
        llm = OpenAICompatLLM(make_config(), sleeper=sleeps.append)

        with mock.patch(
            "agentlab.llm.urllib.request.urlopen",
            side_effect=[error, FakeHTTPResponse(completion_body("恢复了"))],
        ) as patched:
            response = llm.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(response.text, "恢复了")
        self.assertEqual(patched.call_count, 2)
        self.assertEqual(len(sleeps), 1, "重试之间应该等一次")

    def test_401_不重试_直接抛错(self):
        error = urllib.error.HTTPError(
            "https://api.example.com/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error": "invalid key"}'),
        )
        llm = OpenAICompatLLM(make_config(), sleeper=lambda _: None)

        with mock.patch(
            "agentlab.llm.urllib.request.urlopen", side_effect=error
        ) as patched:
            with self.assertRaises(LLMError):
                llm.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(patched.call_count, 1, "key 错了重试没有意义")

    def test_没有_key_构造就失败(self):
        with self.assertRaises(LLMError):
            OpenAICompatLLM(make_config(api_key=""))


class TestOpenAICompatStream(unittest.TestCase):
    def test_增量拼起来等于全文(self):
        llm = OpenAICompatLLM(make_config())
        with mock.patch(
            "agentlab.llm.urllib.request.urlopen",
            return_value=FakeHTTPResponse(lines=sse_lines("你", "好", "！")),
        ):
            chunks = list(llm.stream([{"role": "user", "content": "hi"}]))

        self.assertEqual(chunks, ["你", "好", "！"])
        self.assertEqual("".join(chunks), "你好！")

    def test_注释行和坏片段被跳过(self):
        lines = [
            b": keep-alive\n",
            b"data: not-json\n",
            b"data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode() + b"\n",
            b"data: [DONE]\n",
        ]
        llm = OpenAICompatLLM(make_config())
        with mock.patch(
            "agentlab.llm.urllib.request.urlopen", return_value=FakeHTTPResponse(lines=lines)
        ):
            self.assertEqual(list(llm.stream([{"role": "user", "content": "hi"}])), ["ok"])


class TestFakeLLM(unittest.TestCase):
    def test_按脚本依次返回_用完重复最后一条(self):
        llm = FakeLLM(["第一句", "第二句"])
        messages = [{"role": "user", "content": "hi"}]

        self.assertEqual(llm.chat(messages).text, "第一句")
        self.assertEqual(llm.chat(messages).text, "第二句")
        self.assertEqual(llm.chat(messages).text, "第二句")
        self.assertEqual(llm.call_count, 3)

    def test_记录收到的消息(self):
        llm = FakeLLM(["ok"])
        llm.chat([{"role": "system", "content": "你是助手"}])
        self.assertEqual(llm.last_prompt()[0]["content"], "你是助手")

    def test_流式输出分块(self):
        llm = FakeLLM(["一二三四五六七八"], chunk_size=3)
        chunks = list(llm.stream([{"role": "user", "content": "hi"}]))
        self.assertEqual(chunks, ["一二三", "四五六", "七八"])
        self.assertEqual("".join(chunks), "一二三四五六七八")

    def test_可以传函数作为脚本(self):
        def script(messages):
            last = messages[-1]["content"]
            return f"你说了：{last}"

        llm = FakeLLM(script)
        self.assertEqual(llm.chat([{"role": "user", "content": "吃饭"}]).text, "你说了：吃饭")

    def test_返回_LLMResponse_时保留用量(self):
        llm = FakeLLM([LLMResponse(text="带用量", usage=Usage(1, 2))])
        response = llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.usage.total_tokens, 3)


class TestUsage(unittest.TestCase):
    def test_merge_累加(self):
        total = Usage(1, 2).merge(Usage(3, 4))
        self.assertEqual(total.prompt_tokens, 4)
        self.assertEqual(total.completion_tokens, 6)
        self.assertEqual(total.total_tokens, 10)

    def test_缺字段按0处理(self):
        self.assertEqual(Usage.from_payload({}).total_tokens, 0)
        self.assertEqual(Usage.from_payload(None).total_tokens, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
