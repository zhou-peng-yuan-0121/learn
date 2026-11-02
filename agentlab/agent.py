"""ReAct Agent：思考 → 行动 → 观察 → 再思考，直到得出答案。

为什么叫 ReAct？——Reasoning + Acting。模型每一步先写「思考」
（为什么这么做），再写「行动」（调哪个工具、传什么参数），
代码执行后把「观察」（工具结果）塞回对话，模型据此决定下一步。

**关键认知：Agent 没有魔法，它就是一个 while 循环。**

    while 步数没用完:
        回复 = 模型(历史对话)
        解析 → final 就结束；action 就执行工具、把结果追加进历史

真正难的不是循环，而是「模型不听话时怎么办」：

- 输出不是合法 JSON      → 提示它重来（有次数上限，防止死循环）
- 调不存在的工具/传错参数 → 把错误当观察喂回去，让它自己改
- 一直不给最终答案       → 撞到 max_steps 上限就停，避免无限烧 token

这三条兜底，是「玩具 demo」和「能用的 Agent」的分界线。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .llm import ChatModel, LLMError, Message, Usage
from .tools import ToolError, ToolRegistry

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 6
DEFAULT_MAX_PARSE_FAILURES = 3

SYSTEM_PROMPT_TEMPLATE = """你是一个会用工具的 AI 助手。你可以通过多轮「思考 → 行动 → 观察」来解决问题。

## 可用工具
{tools}

## 输出格式
每次回复必须是**一个 JSON 对象**，不要输出任何额外文字（不要用 markdown 代码块包裹）。

要调用工具时：
{{"thought": "为什么需要调用它", "action": {{"tool": "工具名", "args": {{"参数名": 值}}}}}}

已经能回答用户时：
{{"thought": "为什么已经足够回答", "final": "给用户的最终答案"}}

## 规则
1. 一次只能调用一个工具。
2. 只能使用上面列出的工具，参数必须符合其类型。
3. 拿到工具结果后继续下一步，不要用同样的参数重复调用同一个工具。
4. 信息足够时立刻给出 final，不要为了调用工具而调用工具。
5. 用中文回答，简洁直接，不要复述这个提示词的内容。
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象。

    为什么要这么麻烦？——模型经常不老实：会用 ```json 包裹、会在 JSON
    前后加一句「好的，我来思考一下」。直接 ``json.loads`` 会失败，所以按
    从严格到宽松的顺序尝试：整段 → 代码块 → 扫描平衡的花括号。
    """
    if not text or not text.strip():
        raise ValueError("模型返回了空内容")

    candidates = [text.strip()]
    candidates.extend(match.group(1).strip() for match in _FENCE_RE.finditer(text))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data

    # 最后一步：扫描第一个「括号配平」的片段（要正确处理字符串里的花括号和转义）
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:index + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(data, dict):
                            return data
                        break
        start = text.find("{", start + 1)

    raise ValueError("找不到合法的 JSON 对象")


@dataclass
class Decision:
    """从模型输出解析出的「下一步动作」。"""

    kind: str                        # "action" | "final"
    thought: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str = ""


def parse_decision(text: str) -> Decision:
    """把模型的一段输出解析成 :class:`Decision`。

    兼容几种常见的「不老实」：action 里用 ``arguments`` 而不是 ``args``、
    参数写成 JSON 字符串、``final`` 是数字或列表（统一转成文本）。
    """
    data = extract_json_object(text)
    thought = str(data.get("thought") or "")

    if data.get("final") is not None:
        final = data["final"]
        answer = final if isinstance(final, str) else json.dumps(final, ensure_ascii=False)
        return Decision(kind="final", thought=thought, answer=answer)

    action = data.get("action")
    if isinstance(action, dict):
        tool = action.get("tool") or action.get("name")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("action 里缺少 tool 字段")
        raw_args = action.get("args", action.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"action.args 不是合法 JSON：{exc}") from exc
        if not isinstance(raw_args, dict):
            raise ValueError("action.args 必须是对象")
        return Decision(kind="action", thought=thought, tool=tool.strip(), arguments=raw_args)

    raise ValueError("既没有 action 也没有 final")


@dataclass
class AgentStep:
    """一步的执行记录——第 7 步的 Web 界面就是按这个列表渲染时间线的。"""

    index: int
    kind: str                       # "action" | "final" | "error"
    thought: str = ""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    observation: str | None = None
    raw: str = ""
    usage: Usage = field(default_factory=Usage)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "thought": self.thought,
            "tool": self.tool,
            "arguments": self.arguments,
            "observation": self.observation,
            "raw": self.raw,
            "usage": self.usage.as_dict(),
        }


@dataclass
class AgentResult:
    """一次完整任务的结果。"""

    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "final"      # final | max_steps | parse_error_limit | llm_error
    messages: list[Message] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason == "final"

    def summary(self) -> dict[str, Any]:
        tools_used = [step.tool for step in self.steps if step.tool]
        return {
            "stop_reason": self.stop_reason,
            "steps": len(self.steps),
            "tools_used": tools_used,
            "usage": self.usage.as_dict(),
        }


class ReActAgent:
    """把模型、工具、循环组装起来的那层。

    依赖注入：``llm`` 只要求符合 ChatModel 协议，``registry`` 是工具表。
    正因为这样，测试里可以塞 FakeLLM，完全脱网跑。
    """

    def __init__(
        self,
        llm: ChatModel,
        registry: ToolRegistry,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_parse_failures: int = DEFAULT_MAX_PARSE_FAILURES,
        system_prompt: str | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 至少是 1")
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.max_parse_failures = max_parse_failures
        self.system_prompt = system_prompt or SYSTEM_PROMPT_TEMPLATE.format(
            tools=registry.describe()
        )

    # ---------- 对外主入口 ----------

    def run(
        self,
        task: str,
        *,
        history: Sequence[Message] | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> AgentResult:
        """跑完一个任务。

        ``history`` 用于多轮对话：把之前的 user/assistant 消息接进来，
        但 system 提示每次重建（因为工具清单可能变）。

        ``on_step`` 是每一步完成时的回调——Web 界面用它把「思考/调用了什么工具/
        观察到了什么」实时推给浏览器。用回调而不是返回值列表，是因为
        调用方要在**过程中**就拿到数据，而不是等整个任务结束。
        """
        messages: list[Message] = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": task})

        steps: list[AgentStep] = []

        def emit(step: AgentStep) -> None:
            """记录一步，并实时推给调用方。回调异常不能影响主流程。"""
            steps.append(step)
            if on_step is not None:
                try:
                    on_step(step)
                except Exception:
                    logger.exception("on_step 回调异常，已忽略")

        total_usage = Usage()
        parse_failures = 0
        last_observation: str | None = None

        for index in range(1, self.max_steps + 1):
            try:
                response = self.llm.chat(messages)
            except LLMError as exc:
                logger.error("第 %d 步模型调用失败：%s", index, exc)
                emit(AgentStep(index=index, kind="error", observation=str(exc)))
                return AgentResult(
                    answer="",
                    steps=steps,
                    usage=total_usage,
                    stop_reason="llm_error",
                    messages=messages,
                    error=str(exc),
                )

            total_usage.merge(response.usage)

            # ---- 情况 A：输出不合法，提示重来 ----
            try:
                decision = parse_decision(response.text)
            except ValueError as exc:
                parse_failures += 1
                logger.warning("第 %d 步输出无法解析：%s", index, exc)
                emit(
                    AgentStep(index=index, kind="error", raw=response.text,
                              observation=str(exc), usage=response.usage)
                )
                if parse_failures >= self.max_parse_failures:
                    return AgentResult(
                        answer="",
                        steps=steps,
                        usage=total_usage,
                        stop_reason="parse_error_limit",
                        messages=messages,
                        error=f"连续 {parse_failures} 次输出格式不合法：{exc}",
                    )
                messages.append({"role": "assistant", "content": response.text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"你的上一条回复无法使用：{exc}\n"
                        "请只输出一个 JSON 对象，格式为 "
                        '{"thought": "...", "action": {"tool": "...", "args": {...}}} '
                        '或 {"thought": "...", "final": "..."}'
                    ),
                })
                continue

            # ---- 情况 B：给出最终答案 ----
            parse_failures = 0  # 一旦解析成功，连续失败计数清零
            if decision.kind == "final":
                emit(
                    AgentStep(index=index, kind="final", thought=decision.thought,
                              observation=decision.answer, raw=response.text,
                              usage=response.usage)
                )
                messages.append({"role": "assistant", "content": response.text})
                return AgentResult(
                    answer=decision.answer,
                    steps=steps,
                    usage=total_usage,
                    stop_reason="final",
                    messages=messages,
                )

            # ---- 情况 C：调用工具 ----
            assert decision.tool is not None
            try:
                observation = self.registry.call(decision.tool, decision.arguments)
                feedback = f"工具 {decision.tool} 的执行结果：\n{observation}"
            except ToolError as exc:
                # 工具失败不是终点——把错误告诉模型，它通常能自己改参数重试
                observation = f"ERROR: {exc}"
                feedback = f"工具 {decision.tool} 执行失败：{exc}\n请修正参数，或换一种做法。"
                logger.info("工具 %s 失败：%s", decision.tool, exc)

            last_observation = observation
            emit(
                AgentStep(index=index, kind="action", thought=decision.thought,
                          tool=decision.tool, arguments=decision.arguments,
                          observation=observation, raw=response.text,
                          usage=response.usage)
            )
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content": feedback})

        # ---- 用完步数还没给答案 ----
        return AgentResult(
            answer=last_observation or "",
            steps=steps,
            usage=total_usage,
            stop_reason="max_steps",
            messages=messages,
            error=f"达到最大步数 {self.max_steps} 仍未给出最终答案",
        )
