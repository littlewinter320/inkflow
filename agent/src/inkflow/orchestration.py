"""共享 LangGraph 工具运行器：条件分支、节点检查点和本地诊断。"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langchain.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .errors import ValidationGateError
from .observability import metric_span


class ToolGraphState(TypedDict):
    """只保存程序节点状态；小说与审核原件由原服务按版本管理。"""
    next_tool: str
    steps: int
    last_tool: str


async def run_tool_graph(*, operations: dict[str, Callable[[], Awaitable[str | None]]],
                         first: str, checkpoint_path: Path, max_steps: int,
                         on_event: Callable[[str, str], None] | None = None) -> None:
    """运行授权工具集合；工具依据真实返回选后继，不替其审核或重试副作用。"""
    if first not in operations or type(max_steps) is not int or max_steps < 1:
        raise ValidationGateError("图调度缺少有效入口或执行边界。")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in operations):
        raise ValidationGateError("图调度工具名称不在程序命名边界内。")
    builder = StateGraph(ToolGraphState)

    def make_node(name: str, operation: Callable[[], Awaitable[str | None]]):
        """每个业务工具是独立图节点，LangChain 只接触无正文的控制结果。"""
        bound_tool = tool(name, description=f"执行已授权的 {name} 节点，返回允许的下一步工具名称。")(operation)

        async def invoke(state: ToolGraphState) -> ToolGraphState:
            """先检查步数与当前指针，再调用；中断不在框架层自动重放。"""
            if state["next_tool"] != name or state["steps"] >= max_steps:
                raise ValidationGateError("图调度已达到本次节点边界，原产物与断点保留。")
            if on_event:
                on_event(name, "running")
            with metric_span(f"graph.{name}") as metrics:
                next_tool = await bound_tool.ainvoke({}, config={"callbacks": []})
                if next_tool is not None and next_tool not in operations:
                    raise ValidationGateError("工具选择了未授权的后继节点，未继续执行。")
                metrics["steps"] = state["steps"] + 1
            if on_event:
                on_event(name, "completed")
            return {"next_tool": next_tool or END, "steps": state["steps"] + 1, "last_tool": name}

        return invoke

    def next_node(state: ToolGraphState) -> str:
        """只能跳转到本次显式注册的节点或正常结束。"""
        return state["next_tool"]

    for name, operation in operations.items():
        builder.add_node(name, make_node(name, operation), retry_policy=None)
        builder.add_conditional_edges(name, next_node, {key: key for key in (*operations, END)})
    builder.add_edge(START, first)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次执行有独立检查点；恢复仍由原服务核原件与提交回执后重新选入口。
    # 不直接 ainvoke(None) 重放未知结果的付费请求，更不把图节点完成视为正文已接受。
    with metric_span("graph.run"):
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = builder.compile(checkpointer=saver)
            await graph.ainvoke({"next_tool": first, "steps": 0, "last_tool": ""},
                config={"configurable": {"thread_id": uuid4().hex}, "callbacks": [],
                        "recursion_limit": max_steps + 2})
