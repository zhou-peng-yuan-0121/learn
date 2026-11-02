"""内置工具：先用几个安全的示例工具把协议跑通。

真正的知识库工具（检索、录入）在第 5、6 步实现；这里放的是
「任何 Agent 都可能用到、且能体现安全边界」的两个工具：

- ``calculator``：用 AST 白名单算数，**绝不用 eval**；
- ``current_time``：让模型知道现在几点（模型自己不知道今天几号）。
"""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from typing import Any, Callable

from .tools import ToolError, ToolRegistry, tool

# ---------------------------------------------------------------- 计算器

_MAX_EXPRESSION_LENGTH = 512
_MAX_NODES = 64

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 允许出现的名字：常量 + 少量数学函数。
# 注意这里没有任何能接触文件/网络/反射的东西。
_SAFE_NAMES: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "hypot": math.hypot,
    "factorial": math.factorial,
}

# 每个函数的参数个数上限，防止 factorial(99999999) 这种把机器算死
_SAFE_ARITY: dict[str, tuple[int, int]] = {
    "abs": (1, 1),
    "round": (1, 2),
    "min": (1, 8),
    "max": (1, 8),
    "sqrt": (1, 1),
    "log": (1, 2),
    "log10": (1, 1),
    "exp": (1, 1),
    "floor": (1, 1),
    "ceil": (1, 1),
    "sin": (1, 1),
    "cos": (1, 1),
    "tan": (1, 1),
    "hypot": (2, 8),
    "factorial": (1, 1),
}


def _eval_node(node: ast.AST) -> Any:
    """递归求值——只认白名单里的节点类型，其他一律拒绝。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ToolError("只支持数字")
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))

    if isinstance(node, ast.Name):
        value = _SAFE_NAMES.get(node.id)
        if value is None:
            raise ToolError(f"不支持的名字：{node.id}")
        if callable(value):
            raise ToolError(f"{node.id} 是函数，要调用它，例如 {node.id}(2)")
        return value

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id
        func = _SAFE_NAMES.get(fname)
        if func is None or not callable(func):
            raise ToolError(f"不支持的函数：{fname}")
        if node.keywords:
            raise ToolError("不支持关键字参数")
        args = [_eval_node(arg) for arg in node.args]
        low, high = _SAFE_ARITY.get(fname, (0, 8))
        if not low <= len(args) <= high:
            raise ToolError(f"{fname} 需要 {low}~{high} 个参数，收到 {len(args)} 个")
        return func(*args)

    raise ToolError(f"不支持的语法：{type(node).__name__}")


def safe_eval(expression: str) -> int | float:
    """在白名单下计算表达式。

    为什么不用 ``eval()``？——``eval("__import__('os').system('rm -rf /')")``
    是真实存在的攻击面。工具是模型唯一能"动手"的地方，必须假设模型可能被骗。
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("expression 不能为空")
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise ToolError(f"表达式太长（上限 {_MAX_EXPRESSION_LENGTH} 字符）")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"表达式语法错误：{exc.msg}") from exc

    if sum(1 for _ in ast.walk(tree)) > _MAX_NODES:
        raise ToolError("表达式太复杂")

    try:
        result = _eval_node(tree)
    except ZeroDivisionError as exc:
        raise ToolError("除以零了") from exc
    except OverflowError as exc:
        raise ToolError("结果溢出（数字太大了）") from exc

    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ToolError("结果不是数字")
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise ToolError("结果不是有限数字")
    return result


@tool(name="calculator", description="计算数学表达式，例如 '12 * (3 + 4)' 或 'sqrt(2) + pi'")
def calculator(expression: str) -> str:
    """计算一个数学表达式。"""
    value = safe_eval(expression)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


# ---------------------------------------------------------------- 时间

@tool(name="current_time", description="获取当前本地日期和时间（模型自己不知道今天几号）")
def current_time(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回当前时间，fmt 是 strftime 格式串。"""
    try:
        return datetime.now().strftime(fmt)
    except ValueError as exc:
        raise ToolError(f"时间格式不合法：{exc}") from exc


def default_registry() -> ToolRegistry:
    """默认工具集——第 4 步的 Agent 会用它。"""
    return ToolRegistry([calculator, current_time])
