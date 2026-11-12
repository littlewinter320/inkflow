"""One request-scoped budget and public event stream for nested workflows."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from dataclasses import dataclass, field
import time
import warnings
from typing import Any, Callable

from .errors import ProviderError
from .model_usage import normalized_usage


COMPLETION_ATTEMPT_LIMIT = 10  # 同任务同节点的累计处理上限；续接不重置历史次数。


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
    unknown_estimate_calls: int = 0
    output_policy: str = "provider_default"
    output_policy_source: str = "current_application_policy"
    task_id: str = ""
    run_id: str = ""
    request_priority: int = 1
    # 附件与父执行片段共用持久账本上限；run_id仍独立用于事件与恢复。
    budget_source_run_id: str = ""
    attempt_fragments: list[dict[str, Any]] = field(default_factory=list)
    role_attempt_limits: dict[str, int] = field(default_factory=dict)
    # 仅由原报告续接入口装入的显式单次编辑授权；不进入公开事件或配置快照。
    # 实际发送仍由 role_attempt_limits 和持久化账本共同拦截，不能靠此字段重置次数。
    editor_review_grant: dict[str, Any] | None = None
    # 仅显式原报告续接装入的单次记忆补核，沿原账本限制所有格式及网络发送。
    memory_review_grant: dict[str, Any] | None = None
    # 工具栏只授权接收点击时已保存的版本，异步等待后也不能悄悄改为另一稿。
    chapter_acceptance_source: dict[str, Any] | None = None
    started: float = field(default_factory=time.monotonic)

    def reserve(self, estimated_tokens: int | None) -> int | None:
        if self.calls >= self.max_calls or (self.max_tokens is not None and (
                estimated_tokens is None or self.unknown_estimate_calls or self.tokens + estimated_tokens > self.max_tokens)):
            raise RunBudgetExceeded("本次执行片段已达到调用或原始 Token 预算，进度已保留；继续任务会自动从断点恢复。")
        self.calls += 1
        if estimated_tokens is None:
            self.unknown_estimate_calls += 1
        else:
            self.tokens += estimated_tokens
        self.unknown_calls += 1
        return estimated_tokens

    def settle(self, reserved: int | None, usage: dict[str, Any], *, anthropic: bool = False) -> None:
        """Settle the observed usage once; notification failure must not replay a paid request."""
        counts = normalized_usage(usage, anthropic=anthropic)
        prompt, output = counts["input_tokens"], counts["output_tokens"]
        total = prompt + output if prompt is not None and output is not None else None
        if total is None and type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
            total = usage["total_tokens"]
        if total is not None:
            self.tokens += total - (reserved if reserved is not None else 0)
            if reserved is None:
                self.unknown_estimate_calls -= 1
            self.unknown_calls -= 1
        try:
            self.publish({"type": "usage.updated", "summary": f"本次已请求 {self.calls} 次模型", "metadata": self.snapshot()})
        except Exception as exc:
            warnings.warn(f"用量通知暂未送达（{type(exc).__name__}）；已保留本次结算。", RuntimeWarning, stacklevel=2)

    def record_attempt(self, fragment: dict[str, Any]) -> None:
        """Expose unsuccessful/unknown attempts without modifying the reserved budget."""
        if any(item["attempt_id"] == fragment["attempt_id"] for item in self.attempt_fragments):
            return
        self.attempt_fragments.append(fragment)
        self.publish({"type": "usage.attempt_finished", "summary": "模型尝试已记录，失败与未知用量保留",
                      "metadata": fragment})

    def snapshot(self) -> dict[str, Any]:
        return {"calls": self.calls, "tokens": None if self.unknown_estimate_calls else self.tokens,
                "known_or_reserved_tokens": self.tokens, "unknown_calls": self.unknown_calls,
                "unknown_estimate_calls": self.unknown_estimate_calls,
                "output_policy": self.output_policy, "output_policy_source": self.output_policy_source,
                "max_calls": self.max_calls, "max_tokens": self.max_tokens,
                "role_attempt_limits": dict(self.role_attempt_limits),
                "request_priority": self.request_priority,
                "failed_calls": sum(item["outcome"] == "failed" for item in self.attempt_fragments),
                "cancelled_calls": sum(item["outcome"] == "cancelled" for item in self.attempt_fragments),
                "sent_calls": sum(item.get("request_sent") is True for item in self.attempt_fragments),
                "not_sent_calls": sum(item.get("request_sent") is False for item in self.attempt_fragments),
                "elapsed_ms": max(0, round((time.monotonic() - self.started) * 1000)),
                "attempt_fragments": list(self.attempt_fragments),
                "token_note": "unknown_calls 保留预算估计；未知实际用量不算零费用。"}


active_runtime: ContextVar[RunRuntime | None] = ContextVar("inkflow_runtime", default=None)
active_generation_attempt_limit: ContextVar[int | None] = ContextVar("inkflow_generation_attempt_limit", default=None)
active_generation_request_id: ContextVar[str | None] = ContextVar("inkflow_generation_request_id", default=None)


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
