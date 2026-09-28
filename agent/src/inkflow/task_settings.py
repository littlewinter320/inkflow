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

from .config import PERSISTED_SETTING_NAMES, Settings
from .errors import InkFlowError, ProjectError
from .role_protocol import roles_for_mode
from .utils import content_hash, utc_now


TASK_SETTINGS_SCHEMA_VERSION = 2
TASK_SETTINGS_FIELDS = frozenset(PERSISTED_SETTING_NAMES) - {
    "input_price_per_million", "output_price_per_million"
}
_SNAPSHOT_FIELDS_V1 = frozenset({
    "schema_version", "task_id", "novel_id", "captured_at", "source", "settings", "snapshot_hash"
})
_SNAPSHOT_FIELDS_V2 = _SNAPSHOT_FIELDS_V1 | {"role_protocol_version", "collaboration_mode"}
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
    role_protocol_version: int
    collaboration_mode: str
    settings: Settings

    def public_summary(self) -> dict[str, Any]:
        summary = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "novel_id": self.novel_id,
            "snapshot_hash": self.snapshot_hash,
            "captured_at": self.captured_at,
            "source": self.source,
        }
        if self.schema_version >= 2:
            summary.update({
                "role_protocol_version": self.role_protocol_version,
                "collaboration_mode": self.collaboration_mode,
            })
        return summary


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
    role_protocol_version: int = 1, collaboration_mode: str = "everyday",
) -> dict[str, Any]:
    _check_role_mode(role_protocol_version, collaboration_mode)
    values = {key: value for key, value in settings.to_mapping().items() if key in TASK_SETTINGS_FIELDS}
    _check_endpoint(values)
    snapshot = {
        "schema_version": TASK_SETTINGS_SCHEMA_VERSION,
        "task_id": task_id if task_id is not None else f"task-{uuid4().hex}",
        "novel_id": novel_id,
        "captured_at": utc_now(),
        "source": source,
        "role_protocol_version": role_protocol_version,
        "collaboration_mode": collaboration_mode,
        "settings": values,
    }
    snapshot["snapshot_hash"] = content_hash(_canonical(snapshot))
    validate_task_settings_snapshot(snapshot, novel_id=novel_id)
    return snapshot


def _check_role_mode(protocol_version: int, mode: str) -> None:
    if type(protocol_version) is not int or protocol_version not in {1, 2}:
        raise TaskSettingsError("任务角色协议版本无效。")
    try:
        roles_for_mode(mode)
    except ValueError as exc:
        raise TaskSettingsError("任务协作模式无效。") from exc
    if protocol_version == 1 and mode != "everyday":
        raise TaskSettingsError("旧角色协议只支持日常模式。")


def validate_task_settings_snapshot(
    snapshot: dict[str, Any], *, novel_id: str | None = None, task_id: str | None = None
) -> dict[str, Any]:
    """Verify the persisted envelope before constructing any runtime settings."""
    if not isinstance(snapshot, dict):
        raise TaskSettingsError("任务配置快照结构不完整，不能用当前设置静默替换。")
    version = snapshot.get("schema_version")
    if type(version) is not int or version not in {1, TASK_SETTINGS_SCHEMA_VERSION}:
        raise TaskSettingsError("任务配置快照版本不受当前程序支持。")
    expected_fields = _SNAPSHOT_FIELDS_V1 if version == 1 else _SNAPSHOT_FIELDS_V2
    if set(snapshot) != expected_fields:
        raise TaskSettingsError("任务配置快照结构不完整，不能用当前设置静默替换。")
    if version == 2:
        _check_role_mode(snapshot["role_protocol_version"], snapshot["collaboration_mode"])
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
    if not isinstance(values, dict) or set(values) != TASK_SETTINGS_FIELDS:
        raise TaskSettingsError("任务配置字段与快照版本不一致，不能补入当前默认值。")
    _check_endpoint(values)
    expected = content_hash(_canonical({key: value for key, value in snapshot.items() if key != "snapshot_hash"}))
    if snapshot["snapshot_hash"] != expected:
        raise TaskSettingsError("任务配置快照校验失败，不能用当前设置回填。")
    summary = {key: snapshot[key] for key in expected_fields if key != "settings"}
    if version == 1:
        summary.update({"role_protocol_version": 1, "collaboration_mode": "everyday"})
    return summary


def restore_task_settings(
    snapshot: dict[str, Any], *, novel_id: str, workspace_root: str | Path
) -> TaskSettingsScope:
    summary = validate_task_settings_snapshot(snapshot, novel_id=novel_id)
    try:
        settings = Settings.from_mapping(snapshot["settings"], workspace_root=workspace_root)
    except (TypeError, ValueError, InkFlowError) as exc:
        raise TaskSettingsError("任务配置快照包含无效设置，无法恢复。") from exc
    # The configuration parser may evolve; silently migrating an existing
    # snapshot would make its hash no longer describe the actual execution.
    restored = {key: value for key, value in settings.to_mapping().items() if key in TASK_SETTINGS_FIELDS}
    if _canonical(restored) != _canonical(snapshot["settings"]):
        raise TaskSettingsError("当前程序会改变已有任务配置，请显式迁移任务后再恢复。")
    return TaskSettingsScope(
        schema_version=summary["schema_version"],
        task_id=summary["task_id"], novel_id=summary["novel_id"],
        snapshot_hash=summary["snapshot_hash"], captured_at=summary["captured_at"],
        source=summary["source"], role_protocol_version=summary["role_protocol_version"],
        collaboration_mode=summary["collaboration_mode"], settings=settings,
    )
