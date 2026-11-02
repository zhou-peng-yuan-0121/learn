"""ReAct 循环的测试 —— 全部用 FakeLLM，脱网、秒级、结果可复现。

这些用例覆盖的是「模型不听话」的各种情况，因为它们才是真实世界里
每天都会遇到的：格式错、工具错、原地打转、调用失败。
"""

from __future__ import annotations

import json
import unittest

from agentlab.agent import (
    AgentResult,
    ReActAgent,
    extract_json_object,
    parse_decision,
)
from agentlab.builtin_tools import calculator, default_registry
from agentlab.llm import FakeLLM, LLMError, LLMResponse, Usage
from agentlab.tools import ToolRegistry, tool


def action(tool: str, **args) -> str:
    """构造一条「调用工具」的模型输出。"""
    return json.dumps({"thought": f"需要用 {tool}", "action": {"tool": tool, "args": args}}, ensure_ascii=False)


def final(answer: str) -> str:
    """构造一条「给出答案」的模型输出。"""
    return json.dumps({"thought": "信息够了", "final": answer}, ensure_ascii=False)


@tool(name="echo", description="原样返回输入")
def echo(text: str) -> str:
    """回显。"""
    return text


@tool(name="boom", description="总是失败的工具")
def boom(x: int) -> str:
    """必然抛错，用来验证错误恢复。"""
    raise RuntimeError("内部爆炸")


class FailingLLM:
    """永远抛 LLMError 的假模型，用来验证网络失败时的降级。"""

    name = "failing"

    def chat(self, messages, *, temperature=None):
        raise LLMError("连接超时")

    def stream(self, messages, *, temperature=None):
        raise LLMError("连接超时")


def make_agent(script, *, tools=None, **kwargs) -> ReActAgent:
    registry = tools or default_registry()
    return ReActAgent(FakeLLM(script), registry, **kwargs)


# ---------------------------------------------------------------- JSON 解析

class TestExtractJson(unittest.TestCase):
    def test_纯JSON(self):
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_代码块包裹(self):
        text = '```json\n{"final": "答案"}\n```'
        self.assertEqual(extract_json_object(text)["final"], "答案")

    def test_前后有废话(self):
        text = '好的，我来思考一下：{"thought": "x", "final": "y"} 希望有帮助'
        self.assertEqual(extract_json_object(text)["final"], "y")

    def test_字符串里含花括号不会被截断(self):
        text = '{"thought": "用 {大括号} 测试", "final": "ok"}'
        data = extract_json_object(text)
        self.assertEqual(data["thought"], "用 {大括号} 测试")

    def test_没有JSON要报错(self):
        with self.assertRaises(ValueError):
            extract_json_object("我不会用 JSON")

    def test_空字符串要报错(self):
        with self.assertRaises(ValueError):
            extract_json_object("   ")


class TestParseDecision(unittest.TestCase):
    def test_解析action(self):
        decision = parse_decision(action("calculator", expression="1+1"))
        self.assertEqual(decision.kind, "action")
        self.assertEqual(decision.tool, "calculator")
        self.assertEqual(decision.arguments, {"expression": "1+1"})

    def test_解析final(self):
        decision = parse_decision(final("结果是 2"))
        self.assertEqual(decision.kind, "final")
        self.assertEqual(decision.answer, "结果是 2")

    def test_兼容arguments字段(self):
        text = json.dumps({"thought": "", "action": {"tool": "echo", "arguments": {"text": "hi"}}})
        decision = parse_decision(text)
        self.assertEqual(decision.arguments, {"text": "hi"})

    def test_兼容args是JSON字符串(self):
        text = json.dumps({"thought": "", "action": {"tool": "echo", "args": '{"text": "hi"}'}})
        self.assertEqual(parse_decision(text).arguments, {"text": "hi"})

    def test_final是数字时转成文本(self):
        self.assertEqual(parse_decision('{"thought": "", "final": 42}').answer, "42")

    def test_缺tool要报错(self):
        with self.assertRaises(ValueError):
            parse_decision('{"thought": "", "action": {"args": {}}}')

    def test_既无action也无final要报错(self):
        with self.assertRaises(ValueError):
            parse_decision('{"thought": "我在思考"}')


# ---------------------------------------------------------------- 主循环

class TestReActLoop(unittest.TestCase):
    def test_两步完成_工具结果被喂回模型(self):
        agent = make_agent([action("calculator", expression="3+4"), final("答案是 7")])
        result = agent.run("3+4 等于几")

        self.assertTrue(result.ok)
        self.assertEqual(result.answer, "答案是 7")
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0].tool, "calculator")
        self.assertEqual(result.steps[0].observation, "7")
        self.assertEqual(result.steps[1].kind, "final")

        # 工具结果必须真的进了对话历史，否则模型无从「观察」
        history_text = "\n".join(str(m["content"]) for m in result.messages)
        self.assertIn("3+4", history_text)
        self.assertIn("7", history_text)

    def test_用量被累加(self):
        agent = make_agent([action("calculator", expression="1+1"), final("2")])
        result = agent.run("1+1")
        # FakeLLM 每次给 8/8，两次调用共 32
        self.assertEqual(result.usage.total_tokens, 32)

    def test_工具失败后模型自己纠正(self):
        script = [
            action("boom", x=1),                       # 第一步：工具内部炸了
            action("calculator", expression="2*3"),    # 第二步：换成对的工具
            final("6"),
        ]
        result = make_agent(script, tools=ToolRegistry([calculator, boom])).run("算一下 2*3")

        self.assertTrue(result.ok)
        self.assertIn("ERROR", result.steps[0].observation)
        self.assertIn("内部爆炸", result.steps[0].observation)
        self.assertEqual(result.steps[1].observation, "6")

    def test_调用不存在的工具也能恢复(self):
        script = [action("nonexistent"), action("echo", text="hi"), final("好")]
        result = make_agent(script, tools=ToolRegistry([echo])).run("测试")

        self.assertTrue(result.ok)
        self.assertIn("没有叫", result.steps[0].observation)

    def test_格式错误后重试成功(self):
        script = ["我觉得应该先算一下", final("算好了")]
        result = make_agent(script, max_parse_failures=3).run("测试")

        self.assertTrue(result.ok)
        self.assertEqual(result.steps[0].kind, "error")
        self.assertEqual(result.answer, "算好了")

    def test_连续格式错误达到上限就停止(self):
        result = make_agent(["我就是不说 JSON"], max_parse_failures=2).run("测试")

        self.assertFalse(result.ok)
        self.assertEqual(result.stop_reason, "parse_error_limit")
        self.assertEqual(len(result.steps), 2)

    def test_用完步数就停_不会无限循环(self):
        result = make_agent([action("calculator", expression="1+1")], max_steps=3).run("测试")

        self.assertEqual(result.stop_reason, "max_steps")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.error)

    def test_模型调用失败时不崩溃(self):
        agent = ReActAgent(FailingLLM(), default_registry())
        result = agent.run("测试")

        self.assertEqual(result.stop_reason, "llm_error")
        self.assertIn("连接超时", result.error)

    def test_多轮对话可以带历史(self):
        llm = FakeLLM([final("记得")])
        agent = ReActAgent(llm, default_registry())
        history = [
            {"role": "user", "content": "我叫小王"},
            {"role": "assistant", "content": final("记住了")},
        ]
        result = agent.run("我叫什么", history=history)

        self.assertTrue(result.ok)
        prompt = llm.last_prompt()
        self.assertEqual([m["role"] for m in prompt], ["system", "user", "assistant", "user"])
        self.assertEqual(prompt[1]["content"], "我叫小王")

    def test_系统提示里含工具清单(self):
        agent = make_agent([final("ok")])
        self.assertIn("calculator", agent.system_prompt)
        self.assertIn("current_time", agent.system_prompt)
        self.assertIn("JSON", agent.system_prompt)

    def test_max_steps必须为正(self):
        with self.assertRaises(ValueError):
            make_agent([final("ok")], max_steps=0)


class TestAgentResult(unittest.TestCase):
    def test_summary_汇总(self):
        result = AgentResult(
            answer="ok",
            steps=[],
            usage=Usage(10, 5),
            stop_reason="final",
        )
        summary = result.summary()
        self.assertEqual(summary["stop_reason"], "final")
        self.assertEqual(summary["usage"]["total_tokens"], 15)

    def test_ok_属性(self):
        self.assertTrue(AgentResult(answer="x").ok)
        self.assertFalse(AgentResult(answer="x", stop_reason="max_steps").ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
