"""One request-scoped budget and public event stream for nested workflows."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ProviderError
from .model_usage import normalized_usage


class RunBudgetExceeded(ProviderError):
    """A resumable request-slice boundary, not a failed novel workflow."""


@dataclass
class RunRuntime:
    publish: Callable[[dict[str, Any]], None]
    max_calls: int = 100
    # Only an explicit task limit may cap raw tokens. A Coordinator estimate is
    # not a user-approved stop condition for an otherwise healthy chapter.
    max_tokens: int | None = None
    calls: int = 0
    tokens: int = 0
    unknown_calls: int = 0
    task_id: str = ""
    run_id: str = ""

    def reserve(self, estimated_tokens: int) -> int:
        if self.calls >= self.max_calls or (self.max_tokens is not None and self.tokens + estimated_tokens > self.max_tokens):
            raise RunBudgetExceeded("本次执行片段已达到调用或原始 Token 预算，进度已保留；继续任务会自动从断点恢复。")
        self.calls += 1
        self.tokens += estimated_tokens
        self.unknown_calls += 1
        return estimated_tokens

    def settle(self, reserved: int, usage: dict[str, Any], *, anthropic: bool = False) -> None:
        counts = normalized_usage(usage, anthropic=anthropic)
        prompt, output = counts["input_tokens"], counts["output_tokens"]
        total = prompt + output if prompt is not None and output is not None else None
        if total is None and type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
            total = usage["total_tokens"]
        if total is not None:
            self.tokens += total - reserved
            self.unknown_calls -= 1
        self.publish({"type": "usage.updated", "summary": f"本次已请求 {self.calls} 次模型", "metadata": self.snapshot()})

    def snapshot(self) -> dict[str, int | None]:
        return {"calls": self.calls, "tokens": self.tokens, "unknown_calls": self.unknown_calls,
                "max_calls": self.max_calls, "max_tokens": self.max_tokens}


active_runtime: ContextVar[RunRuntime | None] = ContextVar("inkflow_runtime", default=None)


@contextmanager
def ensure_run_runtime():
    """Reuse a caller's budget, or attribute standalone requests and their retries."""
    existing = active_runtime.get()
    if existing is not None:
        yield existing
        return
    scope_id = f"standalone-{uuid4().hex}"
    from .task_settings import active_task_settings
    task_scope = active_task_settings.get()
    runtime = RunRuntime(publish=lambda event: None,
                         task_id=task_scope.task_id if task_scope else scope_id, run_id=scope_id)
    token = active_runtime.set(runtime)
    try:
        yield runtime
    finally:
        active_runtime.reset(token)
