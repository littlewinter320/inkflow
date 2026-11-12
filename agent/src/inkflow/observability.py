"""可选的 LangSmith 指标出口；正文、提示词、路径和异常原文留在本地。"""
from __future__ import annotations

import math
import os
import re
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from time import perf_counter
from typing import Any, Iterator
from uuid import uuid4

from langsmith import Client, tracing_context


_metric_parent: ContextVar[tuple[Any, Any, str] | None] = ContextVar("inkflow_metric_parent", default=None)


@lru_cache(maxsize=1)
def _metrics_client() -> Client:
    """仅显式开启后构造客户端；异步批处理不阻塞创作等待上传。"""
    return Client(hide_inputs=True, hide_outputs=True, omit_traced_runtime_info=True,
                  auto_batch_tracing=True, timeout_ms=1500, disable_prompt_cache=True)


def _safe_metrics(values: dict[str, Any]) -> dict[str, Any]:
    """只放行可比较的计数与程序枚举，缺报保留 null，拒绝任意日志正文。"""
    output = {}
    for key in ("input_tokens", "output_tokens", "cache_hit_tokens", "duration_ms", "steps", "cache_layout_version"):
        value = values.get(key)
        if value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0:
            output[key] = value
    for key in ("role", "status", "error_type", "provider", "input_contract"):
        value = values.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z_]{1,64}", value):
            output[key] = value
    return output


@contextmanager
def metric_span(name: str, *, role: str = "") -> Iterator[dict[str, Any]]:
    """禁用隐式全量追踪，仅向显式启用的 LangSmith 发送白名单指标。"""
    started, clock = datetime.now(timezone.utc), perf_counter()
    identity, parent = uuid4(), _metric_parent.get()
    trace_id = parent[1] if parent else identity
    order = (parent[2] + "." if parent else "") + started.strftime("%Y%m%dT%H%M%S%fZ") + str(identity)
    token = _metric_parent.set((identity, trace_id, order))
    metrics: dict[str, Any] = {"role": role, "status": "completed"}
    # LANGSMITH_TRACING 等全局环境变量也不能把 LangChain 的工具参数或异常原文外发。
    with tracing_context(enabled=False):
        try:
            yield metrics
        except BaseException as exc:
            metrics.update(status="cancelled" if isinstance(exc, (KeyboardInterrupt, GeneratorExit))
                           or type(exc).__name__ == "CancelledError" else "failed",
                           error_type=type(exc).__name__)
            raise
        finally:
            _metric_parent.reset(token)
            metrics["duration_ms"] = round((perf_counter() - clock) * 1000)
            if os.getenv("INKFLOW_LANGSMITH_ENABLED") == "1":
                # 不默认为用户建立云端账号或猜测凭据；未配密钥时继续本地审计。
                if not os.getenv("LANGSMITH_API_KEY"):
                    warnings.warn("LangSmith 指标未上传：尚未配置 LANGSMITH_API_KEY。", RuntimeWarning)
                else:
                    try:
                        if not re.fullmatch(r"[a-z_.]{1,80}", name):
                            raise ValueError("invalid metric name")
                        _metrics_client().create_run(name=name, inputs={}, outputs={}, run_type="chain",
                            id=identity, trace_id=trace_id, parent_run_id=parent[0] if parent else None,
                            dotted_order=order, start_time=started, end_time=datetime.now(timezone.utc),
                            error=metrics.get("error_type") if metrics["status"] != "completed" else None,
                            project_name=os.getenv("LANGSMITH_PROJECT", "inkflow-metrics"),
                            extra={"metadata": _safe_metrics(metrics)})
                    except Exception as exc:
                        # 监测故障不重发模型，也不把可能含凭据的 SDK 异常文本写入公开记录。
                        warnings.warn(f"LangSmith 指标暂未上传（{type(exc).__name__}）；本地任务继续。", RuntimeWarning)
