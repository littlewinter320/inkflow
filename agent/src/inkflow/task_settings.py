"""Immutable task configuration snapshots, independent of credentials and prices."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from uuid import uuid4

from .config import PERSISTED_SETTING_NAMES, Settings, adapt_settings
from .errors import InkFlowError, ProjectError
from .role_protocol import ROLE_PROTOCOL_VERSION, new_task_mode
from .utils import content_hash, utc_now


TASK_SETTINGS_SCHEMA_VERSION = 2
TASK_SETTINGS_FIELDS = frozenset(PERSISTED_SETTING_NAMES) - {
    "input_price_per_million", "output_price_per_million"
}
_SNAPSHOT_IDENTITY_FIELDS = frozenset({
    "schema_version", "task_id", "novel_id", "captured_at", "source", "settings", "snapshot_hash"
})
_SNAPSHOT_FIELDS = _SNAPSHOT_IDENTITY_FIELDS | {"collaboration_mode"}
_SOURCES = {"task_start", "legacy_recovery"}


class TaskSettingsError(ProjectError):
    """A task cannot silently substitute different settings for its snapshot."""


@dataclass(frozen=True)
class TaskSettingsScope:
    schema_version: int
    task_id: str
    novel_id: str
    snapshot_hash: str
    captured_at: str
    source: str
    collaboration_mode: str
    settings: Settings

    @property
    def role_protocol_version(self) -> int:
        """Obsolete signature compatibility; the runtime has one role contract."""
        return ROLE_PROTOCOL_VERSION

    def public_summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "novel_id": self.novel_id,
            "snapshot_hash": self.snapshot_hash,
            "captured_at": self.captured_at,
            "source": self.source,
            "collaboration_mode": self.collaboration_mode,
        }


active_task_settings: ContextVar[TaskSettingsScope | None] = ContextVar(
    "inkflow_task_settings", default=None
)


@contextmanager
def use_task_settings(scope: TaskSettingsScope) -> Iterator[None]:
    """Temporarily attribute a batch to its task without resetting the run budget."""
    from .runtime import active_runtime

    runtime = active_runtime.get()
    previous_task_id = runtime.task_id if runtime is not None else None
    token = active_task_settings.set(scope)
    try:
        if runtime is not None:
            runtime.task_id = scope.task_id
        yield
    finally:
        if runtime is not None:
            runtime.task_id = previous_task_id
        active_task_settings.reset(token)


def _canonical(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TaskSettingsError("任务配置无法编码为有效快照。") from exc


def _check_endpoint(settings: dict[str, Any]) -> None:
    endpoint = settings.get("base_url")
    if not isinstance(endpoint, str):
        raise TaskSettingsError("任务配置缺少有效的模型接口地址。")
    try:
        parsed = urlsplit(endpoint)
        safe = parsed.scheme in {"http", "https"} and parsed.hostname
        contains_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise TaskSettingsError("模型接口地址无法安全保存到任务配置。") from exc
    if not safe or contains_credentials or parsed.query or parsed.fragment:
        # Never repeat the URL: userinfo or query parameters can contain keys.
        raise TaskSettingsError("任务配置的接口地址不能包含账号密码、查询参数或片段；凭据请使用系统凭据库。")


def capture_task_settings(
    settings: Settings, *, novel_id: str, task_id: str | None = None, source: str = "task_start",
    role_protocol_version: int | None = None, collaboration_mode: str = "everyday",
) -> dict[str, Any]:
    collaboration_mode = _check_mode(collaboration_mode)
    values = {key: value for key, value in settings.to_mapping().items() if key in TASK_SETTINGS_FIELDS}
    _check_endpoint(values)
    snapshot = {
        "schema_version": TASK_SETTINGS_SCHEMA_VERSION,
        "task_id": task_id if task_id is not None else f"task-{uuid4().hex}",
        "novel_id": novel_id,
        "captured_at": utc_now(),
        "source": source,
        "collaboration_mode": collaboration_mode,
        "settings": values,
    }
    snapshot["snapshot_hash"] = content_hash(_canonical(snapshot))
    validate_task_settings_snapshot(snapshot, novel_id=novel_id)
    return snapshot


def _check_mode(mode: str) -> str:
    try:
        return new_task_mode(mode)
    except ValueError as exc:
        raise TaskSettingsError("任务协作模式无效。") from exc


def adapt_task_reference(reference: dict[str, Any]) -> dict[str, Any]:
    """Consume old reference labels while preserving the original signed identity."""
    if not isinstance(reference, dict):
        raise TaskSettingsError("任务配置引用必须是对象。")
    result = dict(reference)
    label = result.pop("role_protocol_version", None)
    if label is not None and (type(label) is not int or label not in {1, 2}):
        raise TaskSettingsError("任务配置引用中的旧角色标签无效。")
    result["collaboration_mode"] = _check_mode(result.get("collaboration_mode", "everyday"))
    return result


def validate_task_settings_snapshot(
    snapshot: dict[str, Any], *, novel_id: str | None = None, task_id: str | None = None
) -> dict[str, Any]:
    """Verify the persisted envelope before constructing any runtime settings."""
    if not isinstance(snapshot, dict):
        raise TaskSettingsError("任务配置快照结构不完整，不能用当前设置静默替换。")
    version = snapshot.get("schema_version")
    if type(version) is not int or version not in {1, TASK_SETTINGS_SCHEMA_VERSION}:
        raise TaskSettingsError("任务配置快照版本不受当前程序支持。")
    input_fields = frozenset(snapshot)
    allowed_shapes = {_SNAPSHOT_FIELDS, _SNAPSHOT_FIELDS | {"role_protocol_version"}}
    if version == 1:
        allowed_shapes.add(_SNAPSHOT_IDENTITY_FIELDS)
    if input_fields not in allowed_shapes:
        raise TaskSettingsError("任务配置快照结构不完整，不能用当前设置静默替换。")
    for key in ("task_id", "novel_id", "captured_at", "snapshot_hash"):
        if not isinstance(snapshot[key], str) or not snapshot[key].strip():
            raise TaskSettingsError("任务配置快照缺少身份或校验信息。")
    if novel_id is not None and snapshot["novel_id"] != novel_id:
        raise TaskSettingsError("任务配置属于另一部小说，不能用于当前作品。")
    if task_id is not None and snapshot["task_id"] != task_id:
        raise TaskSettingsError("任务配置身份与运行关联不一致。")
    if not isinstance(snapshot["source"], str) or snapshot["source"] not in _SOURCES:
        raise TaskSettingsError("任务配置来源无效。")
    values = snapshot["settings"]
    # New optional configuration must never be backfilled into a hashed old snapshot.
    required_fields = TASK_SETTINGS_FIELDS - {"planning_publication_mode", "role_models", "manual_edit_review_enabled"}
    if not isinstance(values, dict) or not required_fields <= set(values) <= TASK_SETTINGS_FIELDS | {"role_settings_version"}:
        raise TaskSettingsError("任务配置字段与快照版本不一致，不能补入当前默认值。")
    _check_endpoint(values)
    expected = content_hash(_canonical({key: value for key, value in snapshot.items() if key != "snapshot_hash"}))
    if snapshot["snapshot_hash"] != expected:
        raise TaskSettingsError("任务配置快照校验失败，不能用当前设置回填。")
    # Consume format labels only after the original, unmodified envelope passes its hash.
    return adapt_task_reference({key: snapshot[key] for key in input_fields if key != "settings"})


def _frozen_values_match(values: dict[str, Any], restored: dict[str, Any]) -> bool:
    """Check every explicit frozen value; added roles use static defaults, never live settings."""
    for name, expected in values.items():
        actual = restored.get(name)
        if name in {"agent_generation", "agent_context_budgets"}:
            if not isinstance(actual, dict):
                return False
            actual = {role: {key: actual.get(role, {}).get(key) for key in fields}
                      for role, fields in expected.items()}
        elif name == "role_models":
            if not isinstance(actual, dict):
                return False
            actual = {role: actual.get(role) for role in expected}
        if _canonical({name: actual}) != _canonical({name: expected}):
            return False
    return True


def restore_task_settings(
    snapshot: dict[str, Any], *, novel_id: str, workspace_root: str | Path
) -> TaskSettingsScope:
    summary = validate_task_settings_snapshot(snapshot, novel_id=novel_id)
    try:
        raw_values = dict(snapshot["settings"])
        # A signed old envelope may identify old role keys without repeating the settings tag.
        if "role_settings_version" not in raw_values and snapshot.get("role_protocol_version") == 1:
            raw_values["role_settings_version"] = 1
        values = adapt_settings(raw_values)
        settings = Settings.from_mapping(values, workspace_root=workspace_root)
    except (TypeError, ValueError, InkFlowError) as exc:
        raise TaskSettingsError("任务配置快照包含无效设置，无法恢复。") from exc
    if not _frozen_values_match(values, settings.to_mapping()):
        raise TaskSettingsError("当前程序会改变已有任务配置中的明确取值，不能用当前设置替代。")
    return TaskSettingsScope(
        schema_version=summary["schema_version"],
        task_id=summary["task_id"], novel_id=summary["novel_id"],
        snapshot_hash=summary["snapshot_hash"], captured_at=summary["captured_at"],
        source=summary["source"],
        collaboration_mode=summary["collaboration_mode"], settings=settings,
    )
