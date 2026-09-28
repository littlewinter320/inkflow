"""Versioned role names and explicit, side-effect-free settings migration."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, cast


LEGACY_ROLE_PROTOCOL_VERSION = 1
ROLE_PROTOCOL_VERSION = 2

AgentRole = Literal["coordinator", "writer", "editor", "reviewer", "memory_keeper"]
CollaborationMode = Literal[
    "everyday", "review_boost", "memory_boost", "deep", "full_specialist"
]

AGENT_ROLES: tuple[AgentRole, ...] = (
    "coordinator", "writer", "editor", "reviewer", "memory_keeper"
)
MODE_ROLES: Mapping[CollaborationMode, tuple[AgentRole, ...]] = MappingProxyType({
    "everyday": ("coordinator", "writer", "editor"),
    "review_boost": ("coordinator", "writer", "editor", "reviewer"),
    "memory_boost": ("coordinator", "writer", "editor", "memory_keeper"),
    "deep": ("coordinator", "writer", "reviewer", "memory_keeper"),
    "full_specialist": AGENT_ROLES,
})
MODE_CHECK_OWNERS: Mapping[CollaborationMode, Mapping[str, AgentRole]] = MappingProxyType({
    "everyday": MappingProxyType({"general": "editor", "memory": "editor"}),
    "review_boost": MappingProxyType({
        "general": "editor", "logic_continuity": "reviewer", "memory": "editor"
    }),
    "memory_boost": MappingProxyType({"general": "editor", "memory": "memory_keeper"}),
    "deep": MappingProxyType({"general": "reviewer", "memory": "memory_keeper"}),
    "full_specialist": MappingProxyType({
        "expression": "editor", "logic_continuity": "reviewer", "memory": "memory_keeper"
    }),
})
_LEGACY_EDITOR_NAMES = frozenset({"reviewer", "reviewer_verifier", "reviewer_judge"})


def _protocol_version(value: int | None) -> int:
    if value is None:
        return LEGACY_ROLE_PROTOCOL_VERSION
    # bool is an int subclass, and 1.0 == 1; neither is a protocol version.
    if type(value) is not int or value not in {LEGACY_ROLE_PROTOCOL_VERSION, ROLE_PROTOCOL_VERSION}:
        raise ValueError("角色协议版本必须是整数 1 或 2。")
    return value


def normalize_role(role: str, protocol_version: int | None = 1) -> AgentRole:
    """Interpret a stored role using its version; engine services are never agents."""
    version = _protocol_version(protocol_version)
    if not isinstance(role, str):
        raise ValueError("Agent 角色必须是受支持的角色名称。")
    if version == LEGACY_ROLE_PROTOCOL_VERSION and role in _LEGACY_EDITOR_NAMES:
        return "editor"
    if role not in AGENT_ROLES:
        raise ValueError(f"不支持的 Agent 角色：{role}。")
    return cast(AgentRole, role)


def roles_for_mode(mode: str) -> tuple[AgentRole, ...]:
    """Return eligible participants, not a requirement to call every role."""
    if not isinstance(mode, str) or mode not in MODE_ROLES:
        raise ValueError(f"不支持的协作模式：{mode}。")
    return MODE_ROLES[cast(CollaborationMode, mode)]


def check_owners_for_mode(mode: str) -> dict[str, AgentRole]:
    """Return an independent copy of the mode's unique check ownership."""
    roles_for_mode(mode)
    return dict(MODE_CHECK_OWNERS[cast(CollaborationMode, mode)])


def migrate_role_settings(
    values: Mapping[str, Mapping[str, Any]],
    *,
    protocol_version: int | None = 1,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Map generation/context setting keys without defaults, validation or writes.

    Explicit canonical fields win over legacy aliases. Other legacy fields are
    retained, while conflicting aliases without an explicit choice are rejected.
    The caller still validates setting values and chooses the task mode.
    """
    version = _protocol_version(protocol_version)
    if not isinstance(values, Mapping):
        raise ValueError("Agent 角色设置必须是对象。")
    warnings: list[str] = []
    if protocol_version is None and any(role in values for role in ("editor", "memory_keeper")):
        warnings.append(
            "未声明角色协议版本，按旧版解释 reviewer 为 Editor；"
            "保留显式角色设置，不据此启用专项角色或加强模式。"
        )

    grouped: dict[AgentRole, list[tuple[str, dict[str, Any]]]] = {}
    for source_role, raw in values.items():
        target_role = normalize_role(source_role, version)
        if not isinstance(raw, Mapping):
            raise ValueError(f"{source_role} 的角色设置必须是对象。")
        grouped.setdefault(target_role, []).append((source_role, dict(raw)))

    migrated: dict[str, dict[str, Any]] = {}
    for target_role, sources in grouped.items():
        explicit = next((raw for role, raw in sources if role == target_role), {})
        merged: dict[str, Any] = {}
        overridden = False
        for source_role, raw in sources:
            if source_role == target_role:
                continue
            for name, value in raw.items():
                if name in explicit:
                    overridden = overridden or explicit[name] != value
                    continue
                if name in merged and merged[name] != value:
                    raise ValueError(
                        f"旧角色设置映射到 {target_role} 时字段 {name} 冲突；"
                        f"请用显式 {target_role} 设置表达选择。"
                    )
                merged[name] = value
        merged.update(explicit)
        migrated[target_role] = merged
        if overridden:
            warnings.append(f"旧角色别名与显式 {target_role} 设置有冲突，已保留显式字段。")
    return migrated, tuple(warnings)
