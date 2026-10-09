"""Persistent per-attempt accounting; no implicit monetary spending limit.

Counts usage even when JSON validation fails. This is a local estimate ledger,
not the provider's invoice. It never stores prompts, prose, credentials or CoT.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import warnings
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, DecimalException, ROUND_CEILING
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

from .config import Settings, load_user_settings, user_settings_path
from .errors import InkFlowError
from .utils import utc_now


def usage_ledger_path() -> Path:
    return user_settings_path().parent / "model-usage.sqlite"


def project_identity(settings: Settings) -> str:
    if settings.workspace_root:
        try:
            manifest = json.loads((settings.workspace_root / ".inkflow" / "project.json").read_text(encoding="utf-8"))
            return str(manifest.get("project_id") or "")
        except (OSError, ValueError, TypeError):
            pass
    return ""


def price_snapshot(settings: Settings, model: str) -> dict[str, Any]:
    """Snapshot explicit CNY rates, or a dated official DeepSeek peak estimate."""
    base = {"currency": "CNY", "model": model, "observed_at": utc_now()}
    input_price, output_price = settings.input_price_per_million, settings.output_price_per_million
    from .task_settings import active_task_settings
    if active_task_settings.get() is not None:
        # Creation settings are frozen, prices are not. Only read the two
        # advisory rate fields; changed model/endpoint settings stay out.
        try:
            current = load_user_settings()
            same_provider = all(
                str(os.getenv(env, current.get(key, expected))).rstrip("/") == str(expected).rstrip("/")
                for key, env, expected in (
                    ("provider_kind", "INKFLOW_PROVIDER", settings.provider_kind),
                    ("base_url", "INKFLOW_BASE_URL", settings.base_url),
                    ("model", "INKFLOW_MODEL", model),
                )
            )
            input_price = float(os.getenv("INKFLOW_INPUT_PRICE_PER_MILLION", current.get("input_price_per_million", 0))) if same_provider else 0
            output_price = float(os.getenv("INKFLOW_OUTPUT_PRICE_PER_MILLION", current.get("output_price_per_million", 0))) if same_provider else 0
        except (InkFlowError, OSError, TypeError, ValueError):
            return {**base, "source": "current_price_unavailable", "kind": "unknown"}
    if model == settings.model and input_price > 0 and output_price > 0:
        return {**base, "source": "user_configured", "kind": "estimate",
                "input": str(input_price), "cache_read": str(input_price),
                "output": str(output_price)}
    endpoint = urlparse(settings.base_url)
    # Do not apply official prices to third-party proxies with similar names.
    official = endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com"
    if official and date(2026, 9, 23) <= date.today() <= date(2026, 10, 23):
        rates = {
            "deepseek-flash": ("2", "0.04", "8"),
            "deepseek-v4-flash": ("2", "0.04", "8"),
            "deepseek-v4-flash-vision-exp": ("2", "0.04", "8"),
            "deepseek-v4-pro": ("9", "0.30", "27"),
        }.get(model)
        if rates:
            return {**base, "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                    "version": "2026-09-23", "kind": "peak_price_estimate",
                    "input": rates[0], "cache_read": rates[1], "output": rates[2]}
    return {**base, "source": "unconfigured", "kind": "unknown"}


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def normalized_usage(usage: dict[str, Any], *, anthropic: bool = False) -> dict[str, int | None]:
    """Raw token counts; discounts and reasoning subcounts never change totals."""
    if anthropic:
        uncached = _count(usage.get("input_tokens"))
        output = _count(usage.get("output_tokens"))
        hit = _count(usage.get("cache_read_input_tokens", 0))
        write = _count(usage.get("cache_creation_input_tokens", 0))
        prompt = uncached + hit + write if None not in (uncached, hit, write) else None
    else:
        prompt = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
        output = _count(usage.get("completion_tokens", usage.get("output_tokens")))
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        hit = _count(usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        miss = _count(usage.get("prompt_cache_miss_tokens"))
        if prompt is not None and hit is None and miss is not None and miss <= prompt:
            hit = prompt - miss
        if prompt is not None and hit is not None and (hit > prompt or (miss is not None and hit + miss != prompt)):
            hit = None
        write = 0
    miss = prompt - hit - write if None not in (prompt, hit, write) else None
    return {"input_tokens": prompt, "output_tokens": output, "cache_hit_tokens": hit,
            "cache_write_tokens": write, "uncached_input_tokens": miss}


def _cost_micros(counts: dict[str, int | None], price: dict[str, Any]) -> int | None:
    prompt, output = counts["input_tokens"], counts["output_tokens"]
    if prompt is None or output is None or price["kind"] == "unknown":
        return None
    # Generic two-rate settings cannot price Anthropic cache writes reliably.
    if counts.get("cache_write_tokens"):
        return None
    hit = counts["cache_hit_tokens"]
    if hit is None and str(price.get("input")) != str(price.get("cache_read")):
        return None  # Unknown cache usage cannot be priced as a zero hit count.
    hit = hit or 0
    # CNY / million tokens times token count equals micro-CNY; round upward.
    try:
        amount = Decimal(prompt - hit) * Decimal(price["input"]) + Decimal(hit) * Decimal(price["cache_read"]) + Decimal(output) * Decimal(price["output"])
        if not amount.is_finite() or not 0 <= amount <= 2**63 - 1:
            return None
        return int(amount.to_integral_value(rounding=ROUND_CEILING))
    except (DecimalException, ValueError, OverflowError):
        # Pricing is advisory. Bad or unrepresentable rates cannot stop prose.
        return None


class UsageLedger:
    def __init__(self, path: Path | None = None):
        self.path = path or usage_ledger_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_attempts (
                    attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, novel_id TEXT NOT NULL, purpose TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, evaluation_version TEXT NOT NULL,
                    provider TEXT NOT NULL, endpoint TEXT NOT NULL, model TEXT NOT NULL, role TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL DEFAULT 'pending',
                    input_estimate INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL,
                    price_json TEXT NOT NULL, usage_json TEXT, usage_state TEXT NOT NULL DEFAULT 'unknown',
                    input_tokens INTEGER, output_tokens INTEGER, cache_hit_tokens INTEGER, cache_write_tokens INTEGER,
                    cost_estimate_micros INTEGER, pending_estimate_micros INTEGER, error_type TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_attempts_novel ON model_attempts(novel_id, purpose);
            """)
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(model_attempts)")}
            if "prompt_family" not in columns:
                db.execute("ALTER TABLE model_attempts ADD COLUMN prompt_family TEXT NOT NULL DEFAULT ''")
            for name, definition in {"diagnostic_json": "TEXT", "duration_ms": "INTEGER", "request_sent": "INTEGER"}.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE model_attempts ADD COLUMN {name} {definition}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def begin(self, settings: Settings, *, request_id: str, model: str, role: str,
              input_estimate: int, max_output_tokens: int, input_bound: int,
              task_id: str = "", prompt_family: str = "", diagnostic: dict[str, Any] | None = None) -> str:
        novel_id = project_identity(settings)
        price = price_snapshot(settings, model)
        pending_cost = _cost_micros({"input_tokens": input_bound, "output_tokens": max_output_tokens,
                                    "cache_hit_tokens": 0, "cache_write_tokens": 0}, price)
        attempt_id = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO model_attempts
                (attempt_id,request_id,task_id,novel_id,purpose,candidate_id,evaluation_version,
                 provider,endpoint,model,role,prompt_family,started_at,input_estimate,max_output_tokens,price_json,pending_estimate_micros)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, request_id, task_id, novel_id, "production", "", "",
                 settings.provider_kind, urlparse(settings.base_url).hostname or "", model, role,
                 prompt_family, utc_now(),
                 input_estimate, max_output_tokens, json.dumps(price), pending_cost))
            safe = {key: str(value) for key, value in (diagnostic or {}).items() if key in {
                "system_contract_hash", "user_prefix_4096_hash", "materials_hash", "quality_hash", "stage"}}
            db.execute("UPDATE model_attempts SET diagnostic_json=? WHERE attempt_id=?", (json.dumps(safe), attempt_id))
        return attempt_id

    def settle(self, attempt_id: str, usage: dict[str, Any], *, anthropic: bool = False) -> None:
        counts = normalized_usage(usage, anthropic=anthropic)
        known = counts["input_tokens"] is not None and counts["output_tokens"] is not None
        # Persist only usage fields, never arbitrary provider metadata.
        allowed = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
                   "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens", "prompt_tokens_details", "completion_tokens_details", "cache_creation"}
        raw = {key: value for key, value in usage.items() if key in allowed}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT price_json,usage_state FROM model_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ValueError("模型尝试记录不存在，不能结算")
            if row["usage_state"] == "reported":
                return  # Idempotent settlement; never add the same usage twice.
            cost = _cost_micros(counts, json.loads(row["price_json"]))
            db.execute("""UPDATE model_attempts SET usage_json=?,usage_state=?,input_tokens=?,output_tokens=?,
                cache_hit_tokens=?,cache_write_tokens=?,cost_estimate_micros=?,
                pending_estimate_micros=CASE WHEN ? THEN NULL ELSE pending_estimate_micros END WHERE attempt_id=?""",
                (json.dumps(raw), "reported" if known else "partial", counts["input_tokens"], counts["output_tokens"],
                 counts["cache_hit_tokens"], counts["cache_write_tokens"], cost, known, attempt_id))

    def finish(self, attempt_id: str, outcome: str, error_type: str = "", *, duration_ms: int | None = None,
               request_sent: bool | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE model_attempts SET finished_at=?,outcome=?,error_type=?,duration_ms=?,request_sent=? WHERE attempt_id=? AND outcome='pending'",
                       (utc_now(), outcome, error_type, _count(duration_ms), int(request_sent) if type(request_sent) is bool else None, attempt_id))

    def summary(self, novel_id: str | None = None) -> dict[str, Any]:
        where, params = (" WHERE novel_id=?", (novel_id,)) if novel_id is not None else ("", ())
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) AS calls,MIN(started_at) AS since,
                COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(cache_hit_tokens),0) AS cache_hit_tokens,
                COALESCE(SUM(CASE WHEN cache_hit_tokens IS NOT NULL THEN input_tokens ELSE 0 END),0) AS cache_reported_input_tokens,
                COALESCE(SUM(usage_state!='reported'),0) AS unknown_usage_calls,
                COALESCE(SUM(cost_estimate_micros IS NULL),0) AS unknown_cost_calls,
                COALESCE(SUM(cost_estimate_micros),0) AS estimated_micros,
                COALESCE(SUM(pending_estimate_micros),0) AS pending_estimated_micros
                FROM model_attempts""" + where, params).fetchone()
        return {**dict(row), "currency": "CNY", "hard_limit_cny": None,
                "estimated_cost": row["estimated_micros"] / 1_000_000,
                "pending_estimated_cost": row["pending_estimated_micros"] / 1_000_000,
                "scope": "novel" if novel_id is not None else "all_projects",
                "price_note": "按请求时价格快照估算；内置DeepSeek参考采用高峰价，未配置或缺失usage不计作零费用。"}

    def recent_deepseek_cache(self, novel_id: str, limit: int = 20,
                              *, qualified_task_products: dict[str, int] | None = None) -> dict[str, Any]:
        """Recent actual HTTP attempts, separate from lifetime trace averages."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT attempt_id,request_id,task_id,role,prompt_family,model,input_tokens,output_tokens,
                          cache_hit_tokens,cache_write_tokens,started_at,outcome,usage_state,error_type,
                          duration_ms,cost_estimate_micros,diagnostic_json,request_sent FROM model_attempts
                   WHERE novel_id=? AND provider='deepseek'
                   ORDER BY started_at DESC,rowid DESC LIMIT ?""",
                (novel_id, max(1, min(int(limit), 100))),
            ).fetchall()
        reported = [row for row in rows if row["input_tokens"] is not None and row["cache_hit_tokens"] is not None]
        hit = sum(min(int(row["input_tokens"]), max(0, int(row["cache_hit_tokens"]))) for row in reported)
        total = sum(max(0, int(row["input_tokens"])) for row in reported)
        by_role: dict[str, dict[str, Any]] = {}
        for role in {str(row["role"]) for row in rows}:
            role_rows = [row for row in rows if row["role"] == role]
            role_reported = [row for row in role_rows if row["input_tokens"] is not None and row["cache_hit_tokens"] is not None]
            role_total = sum(max(0, int(row["input_tokens"])) for row in role_reported)
            role_hit = sum(min(int(row["input_tokens"]), max(0, int(row["cache_hit_tokens"]))) for row in role_reported)
            by_role[role] = {"calls": len(role_rows), "reported_calls": len(role_reported),
                             "unknown_calls": len(role_rows) - len(role_reported),
                             "input_tokens": role_total, "hit_tokens": role_hit,
                             "miss_tokens": max(0, role_total - role_hit),
                             "hit_rate": round(role_hit / role_total, 4) if role_total else None}
        by_family: dict[str, dict[str, Any]] = {}
        for family in {str(row["prompt_family"] or "legacy/unknown") for row in rows}:
            family_rows = [row for row in rows if str(row["prompt_family"] or "legacy/unknown") == family]
            family_reported = [row for row in family_rows if row["input_tokens"] is not None
                               and row["cache_hit_tokens"] is not None]
            family_total = sum(max(0, int(row["input_tokens"])) for row in family_reported)
            family_hit = sum(min(int(row["input_tokens"]), max(0, int(row["cache_hit_tokens"])))
                             for row in family_reported)
            by_family[family] = {"calls": len(family_rows), "reported_calls": len(family_reported),
                                 "input_tokens": family_total, "hit_tokens": family_hit,
                                 "miss_tokens": max(0, family_total - family_hit),
                                 "hit_rate": round(family_hit / family_total, 4) if family_total else None}
        return {
            "calls": len(rows), "reported_calls": len(reported), "unknown_calls": len(rows) - len(reported),
            "input_tokens": total, "hit_tokens": hit, "miss_tokens": max(0, total - hit),
            "hit_rate": round(hit / total, 4) if total else None,
            "last_at": rows[0]["started_at"] if rows else None,
            "by_role": by_role,
            "by_family": by_family,
            "performance_diagnostic": self._performance_diagnostic([dict(row) for row in rows]),
            "qualified_production": self._qualified_production_cost(novel_id, qualified_task_products),
        }

    @staticmethod
    def _attempt_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
        raw = [row for row in rows if row["input_tokens"] is not None]
        output = [row for row in rows if row["output_tokens"] is not None]
        cache = [row for row in raw if row["cache_hit_tokens"] is not None and row["cache_write_tokens"] is not None]
        cost = [row for row in rows if row["cost_estimate_micros"] is not None]
        timed = [row for row in rows if row["duration_ms"] is not None]
        return {"calls": len(rows), "request_count": len({row["request_id"] for row in rows}),
                "sent_calls": sum(row.get("request_sent") == 1 for row in rows),
                "not_sent_calls": sum(row.get("request_sent") == 0 for row in rows),
                "unknown_sent_calls": sum(row.get("request_sent") is None for row in rows),
                "failed_calls": sum(row["outcome"] == "failed" for row in rows),
                "cancelled_calls": sum(row["outcome"] == "cancelled" for row in rows),
                "parsed_output_calls": sum(row["outcome"] == "completed" for row in rows),
                "unknown_usage_calls": sum(row["usage_state"] != "reported" for row in rows),
                "cache_unknown_calls": len(rows) - len(cache),
                "raw_input_tokens": sum(row["input_tokens"] for row in raw) if raw else None,
                "cached_input_tokens": sum(row["cache_hit_tokens"] for row in cache) if cache else None,
                "uncached_input_tokens": sum(row["input_tokens"] - row["cache_hit_tokens"] - row["cache_write_tokens"] for row in cache) if cache else None,
                "output_tokens": sum(row["output_tokens"] for row in output) if output else None,
                "attempt_elapsed_ms": sum(row["duration_ms"] for row in timed) if timed else None,
                "unknown_duration_calls": len(rows) - len(timed),
                "known_estimated_cost": sum(row["cost_estimate_micros"] for row in cost) / 1_000_000,
                "unknown_cost_calls": len(rows) - len(cost)}

    @classmethod
    def _performance_diagnostic(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            try:
                diagnostic = json.loads(row["diagnostic_json"] or "{}")
            except (ValueError, TypeError):
                diagnostic = {}
            row["diagnostic"] = diagnostic if isinstance(diagnostic, dict) else {}
            key = tuple(str(row["diagnostic"].get(name) or "") for name in
                        ("stage", "materials_hash", "quality_hash", "system_contract_hash"))
            groups.setdefault((str(row["prompt_family"]), *key), []).append(row)
        comparisons = []
        writers = [row for row in reversed(rows) if row["role"] == "writer"]
        for before, after in zip(writers, writers[1:]):
            b, a = before["diagnostic"], after["diagnostic"]
            same = before["prompt_family"] == after["prompt_family"] and all(
                a.get(name) and a.get(name) == b.get(name) for name in
                ("stage", "materials_hash", "quality_hash", "system_contract_hash"))
            rates = [row["cache_hit_tokens"] / row["input_tokens"]
                     if row["input_tokens"] and row["cache_hit_tokens"] is not None else None for row in (before, after)]
            comparisons.append({"before_attempt_id": before["attempt_id"], "after_attempt_id": after["attempt_id"],
                "same_request_retry": before["request_id"] == after["request_id"], "same_scope": bool(same),
                "before_hit_rate": rates[0], "after_hit_rate": rates[1],
                "observed_decline": None if None in rates else rates[1] < rates[0],
                "system_changed": a.get("system_contract_hash") != b.get("system_contract_hash"),
                "early_prefix_changed": a.get("user_prefix_4096_hash") != b.get("user_prefix_4096_hash"),
                "materials_changed": a.get("materials_hash") != b.get("materials_hash"),
                "quality_changed": a.get("quality_hash") != b.get("quality_hash"),
                "conclusion": "同材料与质量范围的实际缓存变化，仍需核查合格产物" if same and None not in rates else
                              "材料、质量范围或usage不足，不能据此判定性能退化或提升"})
        return {**cls._attempt_totals(rows), "same_scope_groups": [
                    {"prompt_family": key[0], "stage": key[1], "materials_hash": key[2],
                     "quality_hash": key[3], "system_contract_hash": key[4],
                     "scope_known": all(key), **cls._attempt_totals(value)} for key, value in groups.items()],
                "writer_sequence": comparisons, "attempts": [
                    {key: value for key, value in row.items() if key != "diagnostic_json"} for row in rows],
                "scope": "recent_actual_attempts", "performance_improvement": None,
                "time_note": "attempt_elapsed_ms 是尝试生命周期总耗时，含排队和退避；并发时不能当作端到端墙钟时间。",
                "quality_note": "结构解析成功仅表示 parsed_output_calls；合格产物另需真实正史接受凭据。"}

    def _qualified_production_cost(self, novel_id: str, products: dict[str, int] | None) -> dict[str, Any]:
        if products is None:
            return {"status": "qualification_not_supplied", "accepted_products": None,
                    "estimated_cost_per_product": None, "reason": "未提供核验后的正史接受任务凭据，不以模型输出成功代替合格产物。"}
        products = {task: count for task, count in products.items() if task and type(count) is int and count > 0}
        rows: list[dict[str, Any]] = []
        with self._connect() as db:
            tasks = list(products)
            for start in range(0, len(tasks), 500):
                chunk = tasks[start:start + 500]
                rows.extend(dict(row) for row in db.execute(
                    "SELECT task_id,request_id,input_tokens,output_tokens,cache_hit_tokens,cache_write_tokens,"
                    "outcome,usage_state,duration_ms,cost_estimate_micros,request_sent FROM model_attempts "
                    "WHERE novel_id=? AND task_id IN (" + ",".join("?" for _ in chunk) + ")",
                    (novel_id, *chunk)))
        totals = self._attempt_totals(rows)
        count = sum(products.values())
        missing_tasks = sorted(set(products) - {row["task_id"] for row in rows})
        return {"status": "reported" if rows and not totals["unknown_cost_calls"] and not missing_tasks else "insufficient_usage",
                "accepted_products": count, **totals,
                "missing_usage_task_ids": missing_tasks,
                "estimated_cost_per_product": totals["known_estimated_cost"] / count if count and rows and not totals["unknown_cost_calls"] and not missing_tasks else None,
                "scope": "all_recorded_attempts_of_verified_acceptance_tasks",
                "limitation": "接受任务之外的前置独立写作/审核不自动归入成本；未知用量、缺账本与未关联任务不算零费用。"}


class ModelAttempt:
    """Accounting failures remain visible without inventing zero-cost usage."""

    def __init__(self, settings: Settings, **fields: Any):
        from .runtime import active_runtime
        self.runtime = active_runtime.get()
        self.started = time.monotonic()
        self.fields = fields
        self.model = fields.get("model", "")
        self.provider = settings.provider_kind
        self.counts = normalized_usage({})
        self.finished = False
        self.request_sent = False
        self.ledger: UsageLedger | None = None
        self.attempt_id = ""
        self.failure = ""
        self.outcome = "failed"
        if settings.provider_kind == "ollama" and urlparse(settings.base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
            return  # Local inference is not a remote billing request.
        try:
            self.ledger = UsageLedger()
            self.attempt_id = self.ledger.begin(settings, **fields)
        except (OSError, sqlite3.Error) as exc:
            self._unavailable(exc)

    def _unavailable(self, exc: Exception) -> None:
        message = f"用量账本暂不可用（{type(exc).__name__}），本次费用可能缺失；没有把它记作零费用。"
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        from .runtime import active_runtime
        runtime = active_runtime.get()
        if runtime:
            runtime.publish({"type": "usage.warning", "summary": message})
        self.ledger = None

    def settle(self, usage: dict[str, Any], *, anthropic: bool = False) -> None:
        self.counts = normalized_usage(usage, anthropic=anthropic)
        if self.ledger:
            try:
                self.ledger.settle(self.attempt_id, usage, anthropic=anthropic)
            except (OSError, sqlite3.Error) as exc:
                self._unavailable(exc)

    def mark_request_sent(self) -> None:
        self.request_sent = True

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        duration_ms = max(0, round((time.monotonic() - self.started) * 1000))
        fragment = {"attempt_id": self.attempt_id or "unrecorded-" + uuid4().hex,
                    "request_id": self.fields.get("request_id", ""), "task_id": self.fields.get("task_id", ""),
                    "role": self.fields.get("role", "unspecified"), "model": self.model, "provider": self.provider,
                    "prompt_family": self.fields.get("prompt_family", ""), "outcome": self.outcome,
                    "error_type": self.failure, "duration_ms": duration_ms, **self.counts,
                    "usage_state": "reported" if self.counts["input_tokens"] is not None and self.counts["output_tokens"] is not None else "unknown_or_partial",
                    "ledger_available": self.ledger is not None,
                    "request_sent": self.request_sent,
                    "diagnostic": {key: str(value) for key, value in self.fields.get("diagnostic", {}).items()
                                   if key in {"system_contract_hash", "user_prefix_4096_hash", "materials_hash", "quality_hash", "stage"}}}
        if self.ledger:
            try:
                self.ledger.finish(self.attempt_id, self.outcome, self.failure, duration_ms=duration_ms,
                                   request_sent=self.request_sent)
            except (OSError, sqlite3.Error) as exc:
                self._unavailable(exc)
        if self.runtime:
            fragment["ledger_available"] = self.ledger is not None
            try:
                self.runtime.record_attempt(fragment)
            except Exception as exc:
                # A diagnostics consumer must not turn a paid, completed model
                # response into a provider retry. The fragment was appended first.
                warnings.warn(f"用量片段通知暂未送达（{type(exc).__name__}）；未重试模型请求。", RuntimeWarning, stacklevel=2)
