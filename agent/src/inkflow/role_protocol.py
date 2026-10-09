"""One role contract; legacy labels are consumed only when importing input."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, cast


LEGACY_ROLE_PROTOCOL_VERSION = 1
ROLE_PROTOCOL_VERSION = 2

AgentRole = Literal["coordinator", "writer", "editor", "reviewer", "memory_keeper"]
CollaborationMode = Literal["everyday", "review_boost", "memory_boost", "full_specialist"]

AGENT_ROLES: tuple[AgentRole, ...] = (
    "coordinator", "writer", "editor", "reviewer", "memory_keeper"
)
MODE_ROLES: Mapping[CollaborationMode, tuple[AgentRole, ...]] = MappingProxyType({
    "everyday": ("coordinator", "writer", "editor"),
    "review_boost": ("coordinator", "writer", "editor", "reviewer"),
    "memory_boost": ("coordinator", "writer", "editor", "memory_keeper"),
    "full_specialist": AGENT_ROLES,
})
MODE_CHECK_OWNERS: Mapping[CollaborationMode, Mapping[str, AgentRole]] = MappingProxyType({
    "everyday": MappingProxyType({"general": "editor", "memory": "editor"}),
    "review_boost": MappingProxyType({
        "general": "editor", "logic_continuity": "reviewer", "memory": "editor"
    }),
    "memory_boost": MappingProxyType({"general": "editor", "memory": "memory_keeper"}),
    "full_specialist": MappingProxyType({
        "expression": "editor", "logic_continuity": "reviewer", "memory": "memory_keeper"
    }),
})
ACTIVE_COLLABORATION_MODES = tuple(MODE_ROLES)
_LEGACY_EDITOR_NAMES = frozenset({"reviewer", "reviewer_verifier", "reviewer_judge"})


def normalize_role(role: str, protocol_version: int | None = None) -> AgentRole:
    """Resolve a canonical role; the obsolete argument never changes its meaning."""
    if not isinstance(role, str):
        raise ValueError("Agent 角色必须是受支持的角色名称。")
    if role not in AGENT_ROLES:
        raise ValueError(f"不支持的 Agent 角色：{role}。")
    return cast(AgentRole, role)


def adapt_role(role: str, *, legacy: bool = False) -> AgentRole:
    """Consume an explicitly identified old input once, without runtime dispatch."""
    if legacy and isinstance(role, str) and role in _LEGACY_EDITOR_NAMES:
        return "editor"
    return normalize_role(role)


def new_task_mode(mode: str) -> CollaborationMode:
    """Accept the retired spelling at an input boundary and return one of four modes."""
    canonical = "memory_boost" if mode == "deep" else mode
    if not isinstance(canonical, str) or canonical not in MODE_ROLES:
        raise ValueError(f"不支持的协作模式：{mode}。")
    return cast(CollaborationMode, canonical)


def roles_for_mode(mode: str) -> tuple[AgentRole, ...]:
    """Return eligible participants, not a requirement to call every role."""
    return MODE_ROLES[new_task_mode(mode)]


def check_owners_for_mode(mode: str) -> dict[str, AgentRole]:
    """Return an independent copy of the mode's unique check ownership."""
    return dict(MODE_CHECK_OWNERS[new_task_mode(mode)])


def migrate_role_settings(
    values: Mapping[str, Mapping[str, Any]],
    *,
    protocol_version: int | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Map generation/context setting keys without defaults, validation or writes.

    Explicit canonical fields win over legacy aliases. Other legacy fields are
    retained, while conflicting aliases without an explicit choice are rejected.
    The caller still validates setting values and chooses the task mode.
    """
    if protocol_version is not None and (type(protocol_version) is not int or protocol_version not in {1, 2}):
        raise ValueError("导入设置中的旧角色标签无效。")
    legacy = protocol_version == LEGACY_ROLE_PROTOCOL_VERSION
    if not isinstance(values, Mapping):
        raise ValueError("Agent 角色设置必须是对象。")
    warnings: list[str] = []
    grouped: dict[AgentRole, list[tuple[str, dict[str, Any]]]] = {}
    for source_role, raw in values.items():
        target_role = adapt_role(source_role, legacy=legacy)
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
