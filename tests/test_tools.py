"""工具协议的测试 —— 重点是「自动生成 schema」和「执行层防御」。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from agentlab.builtin_tools import calculator, current_time, default_registry, safe_eval
from agentlab.tools import (
    ToolError,
    ToolRegistry,
    ToolSpec,
    build_parameters,
    make_spec,
    tool,
)


# ---------------------------------------------------------------- 测试用的工具

@tool
def search(query: str, limit: int = 5) -> str:
    """在知识库中检索相关内容。"""
    return f"检索 {query}，最多 {limit} 条"


@tool(name="sum_all", description="把若干数字加起来")
def sum_all(numbers: list[int], scale: float = 1.0) -> float:
    """求和（这个 docstring 的第二行不该被当成说明）。"""
    return sum(numbers) * scale


def returns_dict(query: str) -> dict:
    """返回结构化结果。"""
    return {"query": query, "ok": True}


def returns_none() -> None:
    """什么都不返回。"""
    return None


def raises_value_error(x: int) -> int:
    """故意抛错。"""
    raise ValueError("工具炸了")


# ---------------------------------------------------------------- schema 生成

class TestBuildParameters(unittest.TestCase):
    def test_基本类型和必填项(self):
        params = build_parameters(search)
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["query"])
        self.assertEqual(params["properties"]["query"]["type"], "string")
        self.assertEqual(params["properties"]["limit"]["type"], "integer")
        self.assertEqual(params["properties"]["limit"]["default"], 5)

    def test_列表类型和浮点默认值(self):
        params = build_parameters(sum_all)
        self.assertEqual(params["properties"]["numbers"]["type"], "array")
        self.assertEqual(params["properties"]["scale"]["type"], "number")
        self.assertEqual(params["properties"]["scale"]["default"], 1.0)

    def test_没有默认值的参数是必填(self):
        params = build_parameters(search)
        self.assertIn("query", params["required"])
        self.assertNotIn("limit", params["required"])

    def test_可选类型(self):
        def func(a: str | None = None, b: int | None = None) -> None:
            ...

        params = build_parameters(func)
        # 关键：Optional 不能被误判成 string
        self.assertEqual(params["properties"]["b"]["type"], "integer")
        self.assertEqual(params["required"], [])

    def test_跳过可变参数(self):
        def func(a: str, *args: str, **kwargs: str) -> None:
            ...

        params = build_parameters(func)
        self.assertEqual(list(params["properties"]), ["a"])

    def test_禁止额外字段(self):
        self.assertFalse(build_parameters(search)["additionalProperties"])


class TestToolDecorator(unittest.TestCase):
    def test_用函数名和docstring第一行(self):
        spec = make_spec(search)
        self.assertEqual(spec.name, "search")
        self.assertEqual(spec.description, "在知识库中检索相关内容。")

    def test_显式覆盖名字和说明(self):
        spec = make_spec(sum_all)
        self.assertEqual(spec.name, "sum_all")
        self.assertEqual(spec.description, "把若干数字加起来")

    def test_spec_of_能取回装饰器挂的信息(self):
        from agentlab.tools import spec_of

        spec = spec_of(search)
        self.assertIsInstance(spec, ToolSpec)
        self.assertEqual(spec.name, "search")


# ---------------------------------------------------------------- 注册表

class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry([search, sum_all, returns_dict, returns_none, raises_value_error])

    def test_名字排序(self):
        self.assertEqual(self.registry.names(), ["raises_value_error", "returns_dict", "returns_none", "search", "sum_all"])
        self.assertEqual(len(self.registry), 5)
        self.assertIn("search", self.registry)

    def test_未知工具报错并列出可用工具(self):
        with self.assertRaises(ToolError) as ctx:
            self.registry.get("nope")
        self.assertIn("nope", str(ctx.exception))
        self.assertIn("search", str(ctx.exception))

    def test_describe_生成紧凑清单(self):
        text = self.registry.describe()
        self.assertIn("search(query:string, limit:integer?)", text)
        self.assertIn("在知识库中检索相关内容", text)

    def test_openai_schema_结构正确(self):
        schema = self.registry.openai_schema()
        self.assertEqual(len(schema), 5)
        first = next(item for item in schema if item["function"]["name"] == "search")
        self.assertEqual(first["type"], "function")
        self.assertEqual(first["function"]["parameters"]["required"], ["query"])

    def test_调用成功返回字符串(self):
        self.assertEqual(self.registry.call("search", {"query": "RAG"}), "检索 RAG，最多 5 条")
        self.assertEqual(self.registry.call("search", '{"query": "RAG", "limit": 2}'), "检索 RAG，最多 2 条")

    def test_返回字典时序列化成JSON(self):
        result = self.registry.call("returns_dict", {"query": "x"})
        self.assertEqual(json.loads(result), {"query": "x", "ok": True})

    def test_返回None时是ok(self):
        self.assertEqual(self.registry.call("returns_none", {}), "ok")

    def test_缺少必填参数(self):
        with self.assertRaises(ToolError) as ctx:
            self.registry.call("search", {})
        self.assertIn("query", str(ctx.exception))

    def test_参数不是合法JSON(self):
        with self.assertRaises(ToolError):
            self.registry.call("search", "{not json")

    def test_参数类型不匹配(self):
        with self.assertRaises(ToolError):
            self.registry.call("search", {"query": "x", "unknown": 1})

    def test_工具内部异常被包装成ToolError(self):
        with self.assertRaises(ToolError) as ctx:
            self.registry.call("raises_value_error", {"x": 1})
        self.assertIn("ValueError", str(ctx.exception))
        self.assertIn("工具炸了", str(ctx.exception))

    def test_注册时不加装饰器也能用(self):
        registry = ToolRegistry()
        registry.register(returns_dict, name="special", description="特别说明")
        self.assertEqual(registry.get("special").description, "特别说明")


# ---------------------------------------------------------------- 计算器安全

class TestCalculator(unittest.TestCase):
    def test_正常计算(self):
        self.assertEqual(safe_eval("1 + 2 * 3"), 7)
        self.assertEqual(safe_eval("(1 + 2) * 3"), 9)
        self.assertEqual(safe_eval("10 / 4"), 2.5)
        self.assertAlmostEqual(safe_eval("sqrt(2)"), 1.41421356, places=6)

    def test_整数结果不带小数点(self):
        self.assertEqual(calculator("12 * 34 + 56"), "464")
        self.assertEqual(calculator("100 / 4"), "25")

    def test_拒绝代码注入(self):
        for evil in [
            '__import__("os").system("whoami")',
            'open("/etc/passwd").read()',
            '(1).__class__.__bases__',
            'exec("print(1)")',
            'lambda: 1',
            '[x for x in range(10)]',
        ]:
            with self.subTest(evil=evil):
                with self.assertRaises(ToolError):
                    safe_eval(evil)

    def test_除以零(self):
        with self.assertRaises(ToolError) as ctx:
            safe_eval("1 / 0")
        self.assertIn("零", str(ctx.exception))

    def test_超长表达式被拒(self):
        with self.assertRaises(ToolError):
            safe_eval("1+" * 400 + "1")

    def test_非数字结果被拒(self):
        # 布尔是 int 的子类，必须显式拒绝，否则 True 会被当成 1 返回
        with self.assertRaises(ToolError):
            safe_eval("True")

    def test_通过注册表调用(self):
        registry = default_registry()
        self.assertEqual(registry.call("calculator", {"expression": "2 ** 10"}), "1024")


class TestCurrentTime(unittest.TestCase):
    def test_默认格式可被解析(self):
        text = current_time()
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        self.assertIsInstance(parsed, datetime)

    def test_自定义格式(self):
        text = current_time("%Y")
        self.assertEqual(len(text), 4)

    def test_非法格式报错(self):
        with self.assertRaises(ToolError):
            current_time("%Q")


class TestDefaultRegistry(unittest.TestCase):
    def test_包含两个示例工具(self):
        registry = default_registry()
        self.assertEqual(registry.names(), ["calculator", "current_time"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
