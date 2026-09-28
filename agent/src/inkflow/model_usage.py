"""Persistent per-attempt accounting; no implicit monetary spending limit.

Counts usage even when JSON validation fails. This is a local estimate ledger,
not the provider's invoice. It never stores prompts, prose, credentials or CoT.
"""
from __future__ import annotations

import json
import os
import sqlite3
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException, ROUND_CEILING
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

from .config import Settings, load_user_settings, user_settings_path
from .errors import InkFlowError
from .utils import utc_now


class ValidationQuotaExceeded(InkFlowError):
    """A hard training-validation boundary, never a resumable request slice."""


@dataclass(frozen=True)
class ValidationScope:
    novel_id: str
    candidate_id: str
    evaluation_version: str


training_validation_scope: ContextVar[ValidationScope | None] = ContextVar(
    "inkflow_training_validation", default=None
)


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
    return {"input_tokens": prompt, "output_tokens": output, "cache_hit_tokens": hit, "cache_write_tokens": write}


def _cost_micros(counts: dict[str, int | None], price: dict[str, Any]) -> int | None:
    prompt, output = counts["input_tokens"], counts["output_tokens"]
    if prompt is None or output is None or price["kind"] == "unknown":
        return None
    # Generic two-rate settings cannot price Anthropic cache writes reliably.
    if counts.get("cache_write_tokens"):
        return None
    hit = counts["cache_hit_tokens"] or 0
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
              task_id: str = "", prompt_family: str = "") -> str:
        scope = training_validation_scope.get()
        novel_id = project_identity(settings)
        if scope and (not novel_id or novel_id != scope.novel_id or not scope.candidate_id or not scope.evaluation_version):
            raise ValidationQuotaExceeded("训练验证必须绑定当前小说、冻结候选和评价版本，不能记成日常写作请求。")
        if scope and input_bound + max_output_tokens > 500_000:
            raise ValidationQuotaExceeded("训练验证的保守输入边界与最大输出合计超过50万原始token，请缩小验证材料。")
        price = price_snapshot(settings, model)
        pending_cost = _cost_micros({"input_tokens": input_bound, "output_tokens": max_output_tokens,
                                    "cache_hit_tokens": 0, "cache_write_tokens": 0}, price)
        attempt_id = uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if scope:
                used = db.execute("SELECT COUNT(*) FROM model_attempts WHERE novel_id=? AND purpose='training_validation'", (novel_id,)).fetchone()[0]
                if used >= 5:
                    raise ValidationQuotaExceeded("这部小说的5次远端训练验证请求已用完；日常写作和本地训练仍可继续。")
            db.execute("""INSERT INTO model_attempts
                (attempt_id,request_id,task_id,novel_id,purpose,candidate_id,evaluation_version,
                 provider,endpoint,model,role,prompt_family,started_at,input_estimate,max_output_tokens,price_json,pending_estimate_micros)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, request_id, task_id, novel_id, "training_validation" if scope else "production",
                 scope.candidate_id if scope else "", scope.evaluation_version if scope else "",
                 settings.provider_kind, urlparse(settings.base_url).hostname or "", model, role,
                 prompt_family, utc_now(),
                 input_estimate, max_output_tokens, json.dumps(price), pending_cost))
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

    def finish(self, attempt_id: str, outcome: str, error_type: str = "") -> None:
        with self._connect() as db:
            db.execute("UPDATE model_attempts SET finished_at=?,outcome=?,error_type=? WHERE attempt_id=? AND outcome='pending'",
                       (utc_now(), outcome, error_type, attempt_id))

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
                COALESCE(SUM(pending_estimate_micros),0) AS pending_estimated_micros,
                COALESCE(SUM(purpose='training_validation'),0) AS training_validation_calls
                FROM model_attempts""" + where, params).fetchone()
        return {**dict(row), "currency": "CNY", "hard_limit_cny": None,
                "estimated_cost": row["estimated_micros"] / 1_000_000,
                "pending_estimated_cost": row["pending_estimated_micros"] / 1_000_000,
                "scope": "novel" if novel_id is not None else "all_projects",
                "price_note": "按请求时价格快照估算；内置DeepSeek参考采用高峰价，未配置或缺失usage不计作零费用。"}

    def recent_deepseek_cache(self, novel_id: str, limit: int = 20) -> dict[str, Any]:
        """Recent actual HTTP attempts, separate from lifetime trace averages."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT role,prompt_family,input_tokens,cache_hit_tokens,started_at FROM model_attempts
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
        }


class ModelAttempt:
    """Accounting failure warns during writing; validation quotas stay fail-closed."""

    def __init__(self, settings: Settings, **fields: Any):
        self.ledger: UsageLedger | None = None
        self.attempt_id = ""
        self.failure = ""
        self.outcome = "failed"
        if settings.provider_kind == "ollama" and urlparse(settings.base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
            return  # Local inference does not spend a remote validation slot.
        try:
            self.ledger = UsageLedger()
            self.attempt_id = self.ledger.begin(settings, **fields)
        except (OSError, sqlite3.Error) as exc:
            self._unavailable(exc)

    def _unavailable(self, exc: Exception) -> None:
        message = f"用量账本暂不可用（{type(exc).__name__}），本次费用可能缺失；没有把它记作零费用。"
        if training_validation_scope.get():
            raise ValidationQuotaExceeded("训练验证名额无法可靠读取，暂不发送验证请求。") from exc
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        from .runtime import active_runtime
        runtime = active_runtime.get()
        if runtime:
            runtime.publish({"type": "usage.warning", "summary": message})
        self.ledger = None

    def settle(self, usage: dict[str, Any], *, anthropic: bool = False) -> None:
        if self.ledger:
            try:
                self.ledger.settle(self.attempt_id, usage, anthropic=anthropic)
            except (OSError, sqlite3.Error) as exc:
                self._unavailable(exc)

    def finish(self) -> None:
        if self.ledger:
            try:
                self.ledger.finish(self.attempt_id, self.outcome, self.failure)
            except (OSError, sqlite3.Error) as exc:
                self._unavailable(exc)
