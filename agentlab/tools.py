"""工具协议：把普通 Python 函数变成 Agent 能调用的「工具」。

模型调用工具的全过程只有三步：

1. **给菜单**：系统提示里塞一份工具清单（名字 + 说明 + 参数 JSON Schema）；
2. **点菜**：模型输出 ``{"tool": "calculator", "args": {"expression": "1+2"}}``；
3. **上菜**：你的代码真正执行函数，把结果作为「观察」喂回去。

三个设计原则：

- **schema 自动生成**：从函数签名 + 类型注解反射出来，杜绝「代码和文档不一致」；
- **执行必须防御**：模型的输出是不可信输入，未知工具、参数不匹配都要变成
  可读的错误消息返回给模型，而不是让程序崩掉；
- **工具要安全**：工具是模型唯一能「动手」的地方，计算器不能用 ``eval``。

对比一下手写 schema 的代价：你改了函数参数，得同步改三处（函数、schema、
提示词）。自动生成之后，只剩函数一处。
"""

from __future__ import annotations

import inspect
import json
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

# Python 类型 → JSON Schema 类型
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# ``from __future__ import annotations`` 打开后，注解是字符串而不是类型对象。
# 这是性能优化（延迟求值），代价是要我们自己把字符串映射回来。
_STRING_FALLBACK = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}

_PRIMITIVE_DEFAULTS = (str, int, float, bool, type(None))


class ToolError(RuntimeError):
    """工具相关错误：未知工具、参数不匹配、工具内部抛错。"""


def json_type_of(annotation: Any) -> dict[str, Any]:
    """把 Python 类型注解翻译成 JSON Schema 片段。"""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}

    # 注解是字符串（延迟求值且解析失败时的兜底）
    if isinstance(annotation, str):
        return {"type": _STRING_FALLBACK.get(annotation.strip(), "string")}

    origin = typing.get_origin(annotation)

    # Annotated[str, "说明"] —— 只取里面那层类型
    if origin is typing.Annotated:
        return json_type_of(typing.get_args(annotation)[0])

    # Optional[X] / X | None —— 去掉 None 后按 X 处理。
    # 注意：`X | None` 的 origin 是 types.UnionType，而 `Optional[X]` 是 typing.Union，
    # 两种写法都要认，否则 Optional 类型会被误判成 string。
    if origin is typing.Union or origin is types.UnionType:
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        return json_type_of(options[0]) if options else {"type": "string"}

    if annotation in _JSON_TYPES:
        return {"type": _JSON_TYPES[annotation]}

    if origin in (list, set, tuple):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}

    return {"type": "string"}


def _safe_default(value: Any) -> Any:
    """只有能放进 JSON 的默认值才写进 schema。"""
    return value if isinstance(value, _PRIMITIVE_DEFAULTS) else None


def build_parameters(func: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名生成 ``parameters`` 这一段 JSON Schema。"""
    signature = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # 注解里有无法解析的自定义类型时退回原始注解
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue  # *args / **kwargs 不支持，工具参数必须显式声明

        schema = json_type_of(hints.get(name, param.annotation))
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            default = _safe_default(param.default)
            if default is not None or param.default is None:
                schema = {**schema, "default": default}
        properties[name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _summary(func: Callable[..., Any]) -> str:
    """取 docstring 第一行当工具说明——模型就是靠这句话判断该不该用这个工具。"""
    doc = inspect.getdoc(func) or ""
    first_line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return first_line or func.__name__.replace("_", " ")


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的「身份证」：名字、说明、参数 schema、真正的函数。"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI function calling 的格式。

        为什么是 ``{"type": "function", "function": {...}}`` 这层壳？
        因为协议就是这么定的。这块 schema 可以直接塞进请求体的 ``tools`` 字段，
        也可以拼成文字塞进系统提示（我们用后者，兼容所有模型）。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @property
    def required_params(self) -> list[str]:
        return list(self.parameters.get("required", []))


def make_spec(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> ToolSpec:
    """由普通函数生成 :class:`ToolSpec`。

    如果函数已经被 ``@tool`` 标记过，就沿用那份元数据——否则会出现
    「装饰器里写了 description，换条路径又退回 docstring」这种静默降级。
    """
    existing = getattr(func, "__agentlab_tool__", None)
    has_existing = isinstance(existing, ToolSpec)

    if has_existing and name is None and description is None:
        return existing  # type: ignore[return-value]

    return ToolSpec(
        name=name or (existing.name if has_existing else func.__name__),  # type: ignore[union-attr]
        description=description or (existing.description if has_existing else _summary(func)),  # type: ignore[union-attr]
        parameters=existing.parameters if has_existing else build_parameters(func),  # type: ignore[union-attr]
        func=func,
    )


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """把函数标记成工具，两种写法都支持::

        @tool
        def search(query: str) -> str: ...

        @tool(name="search", description="在本人的知识库里检索")
        def search_impl(query: str) -> str: ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = make_spec(fn, name=name, description=description)
        setattr(fn, "__agentlab_tool__", spec)
        return fn

    if func is not None:
        return decorator(func)
    return decorator


def spec_of(func: Callable[..., Any]) -> ToolSpec:
    """取出 @tool 挂上的 spec；没有的话即时生成一个。"""
    existing = getattr(func, "__agentlab_tool__", None)
    if isinstance(existing, ToolSpec):
        return existing
    return make_spec(func)


class ToolRegistry:
    """工具注册表：收集工具、生成 schema、执行调用。"""

    def __init__(self, tools: Iterable[Callable[..., Any] | ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for item in tools:
            if isinstance(item, ToolSpec):
                self.add(item)
            else:
                self.register(item)

    # ---------- 登记 ----------

    def register(
        self,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> ToolSpec:
        spec = make_spec(func, name=name, description=description)
        self.add(spec)
        return spec

    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    # ---------- 查询 ----------

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "（无）"
            raise ToolError(f"没有叫 {name!r} 的工具，可用工具：{available}") from exc

    def openai_schema(self) -> list[dict[str, Any]]:
        """全部工具的 schema——可直接放进请求体的 tools 字段。"""
        return [spec.to_openai_schema() for spec in self.specs()]

    def describe(self) -> str:
        """把工具清单渲染成给提示词用的纯文本。

        为什么不直接把 JSON schema 塞进提示词？——能塞，但模型读
        ``- search(query:string): 在知识库里检索`` 这种紧凑格式更省 token，
        效果也不差。参数详情仍保留在 schema 里供调试。
        """
        lines: list[str] = []
        for spec in self.specs():
            params = spec.parameters.get("properties", {})
            rendered = []
            for pname, pschema in params.items():
                optional = "?" if pname not in spec.required_params else ""
                rendered.append(f"{pname}:{pschema.get('type', 'string')}{optional}")
            lines.append(f"- {spec.name}({', '.join(rendered)}): {spec.description}")
        return "\n".join(lines) if lines else "- （无可用工具）"

    # ---------- 执行 ----------

    def call(self, name: str, arguments: Mapping[str, Any] | str | None = None) -> str:
        """执行工具，并把结果统一转成字符串。

        为什么统一返回 str？——因为下游要把结果拼回提示词。数字、字典、
        布尔值都统一序列化，Agent 层就不用关心每种工具的返回类型。
        """
        spec = self.get(name)

        # 模型有时给对象，有时给 JSON 字符串，两种都接受
        if isinstance(arguments, str):
            try:
                parsed: dict[str, Any] = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError(f"参数不是合法 JSON：{exc}") from exc
        elif arguments is None:
            parsed = {}
        else:
            parsed = dict(arguments)

        # 先校验必填参数，报错比 Python 的 TypeError 更好懂
        missing = [p for p in spec.required_params if p not in parsed]
        if missing:
            raise ToolError(f"缺少必填参数：{', '.join(missing)}")

        try:
            result = spec.func(**parsed)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"参数不匹配：{exc}") from exc
        except Exception as exc:  # 工具内部崩了，也要让模型看见错误并恢复
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        if result is None:
            return "ok"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
