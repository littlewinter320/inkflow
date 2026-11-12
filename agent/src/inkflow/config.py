from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigurationError
from .role_protocol import (
    ROLE_PROTOCOL_VERSION,
    migrate_role_settings,
    normalize_role,
)


KEYRING_SERVICE = "InkFlow"
KEYRING_ACCOUNT = "deepseek_api_key"
# 软件输入只在超过一千万时拦截；已核实的模型窗口另行约束，旧配置仍保留审计身份。
SOFTWARE_INPUT_TOKEN_LIMIT = 10_000_000
PROVIDER_KINDS = {"deepseek", "mimo", "openai", "anthropic", "gemini", "openrouter", "ollama", "custom"}
PROVIDER_ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "mimo": "MIMO_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "custom": "INKFLOW_API_KEY",
}
SETTINGS_ENV = "INKFLOW_SETTINGS_PATH"
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "max"}
PERSISTED_SETTING_NAMES = {
    "provider_kind",
    "base_url",
    "model",
    "role_models",
    "manual_edit_review_enabled",
    "reasoning_effort",
    "openai_recommended_reasoning",
    "disable_model_reasoning",
    "thinking_policy_version",
    "mimo_retry_policy_version",
    "retrieval_policy_version",
    "prompt_cache_layout_version",
    "agent_thinking",
    "context_soft_tokens",
    "context_hard_tokens",
    "context_input_limit_tokens",
    "context_budget_mode",
    "agent_context_budgets",
    "max_output_tokens",
    "automatic_output_tokens",
    "max_concurrent_model_requests",
    "writer_task_attempt_limit",
    "request_timeout_seconds",
    "planning_timeout_seconds",
    "trace_level",
    "show_provider_reasoning",
    "inquiry_frequency",
    "hook_strategy",
    "quality_preset",
    "quality_custom_context",
    "collaboration_preset",
    "chapter_length_tolerance",
    "review_min_confidence",
    "acceptance_confirmation_mode",
    "planning_publication_mode",
    "planning_window_chapters",
    "dialogue_history_mode",
    "dialogue_history_interval",
    "dialogue_history_limit",
    "agent_generation",
    "review_verification_mode",
    "review_experience_detail",
    "review_local_nli_model",
    "review_judge_model",
    "retrieval_embedding_model",
    "retrieval_reranker_model",
    "powershell_enabled",
    "voice_enabled",
    "voice_input_enabled",
    "voice_output_enabled",
    "voice_auto_read",
    "voice_auto_send",
    "voice_default_profile",
    "voice_speed",
    "voice_volume",
    "voice_pause_scale",
    "voice_input_device",
    "voice_output_device",
    "voice_compute_device",
    "voice_input_engine",
    "voice_dialogue_engine",
    "voice_text_engine",
    "voice_novel_engine",
    "voice_light_asr_model",
    "voice_light_tts_model",
    "voice_sample_rate",
    "voice_segment_chars",
    "voice_cache_limit_mb",
    "voice_debug",
    "voice_data_dir",
    "input_price_per_million",
    "output_price_per_million",
}

DEFAULT_AGENT_GENERATION: dict[str, dict[str, float | int | None]] = {
    "coordinator": {"temperature": 0.25, "top_p": 0.8, "top_k": None},
    "writer": {"temperature": 0.85, "top_p": 0.95, "top_k": None},
    "editor": {"temperature": 0.2, "top_p": 0.8, "top_k": None},
    "reviewer": {"temperature": 0.2, "top_p": 0.8, "top_k": None},
    "memory_keeper": {"temperature": 0.2, "top_p": 0.8, "top_k": None},
}

DEFAULT_AGENT_CONTEXT_BUDGETS: dict[str, dict[str, int]] = {
    "coordinator": {"soft": 96_000, "hard": 160_000},
    "writer": {"soft": 192_000, "hard": 208_000},
    "editor": {"soft": 160_000, "hard": 176_000},
    "reviewer": {"soft": 160_000, "hard": 176_000},
    "memory_keeper": {"soft": 160_000, "hard": 176_000},
}

# Import-name compatibility only; there is no second settings projection.
DEFAULT_ROLE_GENERATION = DEFAULT_AGENT_GENERATION
DEFAULT_ROLE_CONTEXT_BUDGETS = DEFAULT_AGENT_CONTEXT_BUDGETS
CURRENT_THINKING_POLICY_VERSION = 7


def preset_agent_thinking(preset: str, policy_version: int = CURRENT_THINKING_POLICY_VERSION) -> dict[str, dict[str, str]]:
    """返回对应任务版本的思考梯度；旧快照不采用作者新调整的强度。"""
    if policy_version >= 5:
        levels = {
            "economy": ("off", "off", "low", "low", "off"),
            "balanced": ("off", "off", "low", "medium", "low"),
            "quality": ("low", "low", "medium", "medium", "low"),
        }
        if policy_version == 6:
            levels["quality"] = ("low", "low", "medium", "medium", "medium")
        elif policy_version == 5:
            levels["quality"] = ("low", "off", "medium", "medium", "low")
    else:
        levels = {
            "economy": ("off", "off", "medium", "low", "low"),
            "balanced": ("low", "low", "medium", "high", "medium"),
            "quality": ("medium", "medium", "high", "max", "high"),
        }
    return {role: {"coverage": "off" if level == "off" else "all",
                   "effort": "low" if level == "off" else level}
            for role, level in zip(DEFAULT_AGENT_GENERATION, levels[preset], strict=True)}


def creation_preset_mappings(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """按作者确认的逐角色固定预算生成三档；自定义可手调，实际发送另核模型窗口。"""
    specifications = (
        ("economy", "节省", "调度、写作与记忆无思考；编辑与专项低", "low", "low", "evidence",
         {"coordinator": (0.1, 1.0), "writer": (0.7, 1.0), "editor": (0.1, 1.0), "reviewer": (0.1, 1.0), "memory_keeper": (0.05, 1.0)}),
        ("balanced", "平衡", "调度与写作无思考；编辑与记忆低，专项审核中", "medium", "low", "assisted",
         {"coordinator": (0.2, 1.0), "writer": (0.85, 1.0), "editor": (0.15, 1.0), "reviewer": (0.1, 1.0), "memory_keeper": (0.1, 1.0)}),
        ("quality", "高质量", "调度、写作与记忆低；编辑与专项中", "high", "medium", "assisted",
         {"coordinator": (0.2, 1.0), "writer": (0.9, 1.0), "editor": (0.1, 1.0), "reviewer": (0.1, 1.0), "memory_keeper": (0.1, 1.0)}),
    )
    roles = ("coordinator", "writer", "editor", "reviewer", "memory_keeper")
    result = {}
    # 数值逐项来自作者表格，不再按职责比例、样本或百分比二次改写。
    context_presets = {
        "economy": ((100_000, 131_072), (160_000, 262_144), (128_000, 204_800),
                    (128_000, 204_800), (120_000, 196_608)),
        "balanced": ((120_000, 196_608), (240_000, 368_640), (200_000, 327_680),
                     (200_000, 327_680), (192_000, 262_144)),
        "quality": ((160_000, 262_144), (320_000, 512_000), (276_000, 430_080),
                    (276_000, 430_080), (256_000, 409_600)),
    }
    for preset, name, note, effort, inquiry, verification, overrides in specifications:
        generation = {role: {"temperature": temperature, "top_p": top_p, "top_k": None}
                      for role, (temperature, top_p) in overrides.items()}
        budgets = {role: {"soft": soft, "hard": hard}
                   for role, (soft, hard) in zip(roles, context_presets[preset], strict=True)}
        # 通用字段沿用Writer值，角色调用仍读各自预算；模型物理限制由共享发送门禁封顶。
        soft, hard = budgets["writer"]["soft"], budgets["writer"]["hard"]
        basis = {role: {"basis": "author_preset", "sample_count": 0,
                       "model": settings.model_for(role) if settings else None,
                       "meaning": "作者表格指定的固定预算，不随用量样本自动调整；实际输入仍受模型窗口与输出预留限制。"}
                 for role in roles}
        result[preset] = {
            "id": preset, "name": name, "note": note,
            "quality_preset": preset, "quality_custom_context": False,
            "reasoning_effort": effort, "thinking_policy_version": CURRENT_THINKING_POLICY_VERSION,
            "retrieval_policy_version": 3, "inquiry_frequency": inquiry,
            "agent_thinking": preset_agent_thinking(preset),
            "context_soft_tokens": soft, "context_hard_tokens": hard,
            "review_verification_mode": verification, "context_budget_mode": "custom",
            "agent_context_budgets": budgets, "context_budget_basis": basis,
            "agent_generation": generation,
        }
    return result


def apply_creation_preset(settings: Settings, preset: str) -> Settings:
    """Apply a complete preset without saving settings or changing task identity."""
    if preset == "custom":
        return replace(settings, quality_custom_context=True)
    values = creation_preset_mappings(settings).get(preset)
    if values is None:
        raise ConfigurationError("质量与消耗请选择节省、平衡、高质量或自定义。")
    if settings.thinking_policy_version < 7:
        # 旧分段批次尚未冻结的阶段继续采用升级前选档所用的策略。
        values.update(thinking_policy_version=6, agent_thinking=preset_agent_thinking(preset, 6))
    return replace(settings, **{key: value for key, value in values.items()
                                if key not in {"id", "name", "note", "context_budget_basis"}})


def _official_model_limits(base_url: str, model: str) -> dict[str, Any]:
    if urlparse(base_url).hostname == "api.anthropic.com" and model in {
        "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    }:
        return {"model": model, "context_tokens": 1_000_000, "max_output_tokens": 128_000,
                "source": "https://platform.claude.com/docs/en/about-claude/models/overview",
                "checked_at": "2026-10-20"}
    if urlparse(base_url).hostname in {"api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com"} and model in {
        "mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed", "mimo-v2.5", "mimo-v2.5-pro",
    }:
        # MiMo documents 1M without an exact integer; retain a conservative context ceiling.
        return {"model": model, "context_tokens": 1_000_000, "max_output_tokens": 131_072,
                "source": "https://mimo.mi.com/docs/zh-CN/quick-start/summary/model",
                "checked_at": "2026-10-11"}
    # Official API limits checked 2026-10-26; proxies do not inherit these limits.
    if urlparse(base_url).hostname != "api.deepseek.com" or model not in {
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro",
    }:
        return {}
    return {"model": model, "context_tokens": 1_000_000, "max_output_tokens": 393_216,
            "source": "https://api-docs.deepseek.com/api/create-chat-completion/",
            "checked_at": "2026-10-26"}

# Settings can be written by the voice and provider routes at the same time.
# Serializing the read/validate/replace sequence prevents a fast toggle from
# being overwritten by another request that started with an older snapshot.
_SETTINGS_WRITE_LOCK = threading.RLock()


def user_settings_path() -> Path:
    """返回全局非敏感设置路径，不把桌面偏好写入小说项目。"""

    override = os.getenv(SETTINGS_ENV)
    if override:
        return Path(override).expanduser().resolve()
    app_data = os.getenv("APPDATA")
    if app_data:
        return Path(app_data) / "InkFlow" / "settings.json"
    return Path.home() / ".inkflow" / "settings.json"


def load_user_settings() -> dict[str, Any]:
    path = user_settings_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"墨流用户设置无法读取：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("墨流用户设置格式无效，应为 JSON 对象。")
    return adapt_settings({key: value[key] for key in PERSISTED_SETTING_NAMES | {"role_settings_version"}
                           if key in value})


def adapt_settings(value: dict[str, Any]) -> dict[str, Any]:
    """Consume explicit old role labels once; unlabelled input is canonical."""
    if not isinstance(value, dict):
        raise ConfigurationError("设置必须是对象。")
    result = dict(value)
    old_label = result.pop("role_settings_version", None)
    # Validate an obsolete label even when this particular patch has no role fields.
    _migrate_settings_roles({}, old_label)
    for name in ("agent_generation", "agent_context_budgets"):
        if name in result:
            result[name], _ = _migrate_settings_roles(result[name], old_label)
    if "role_models" in result:
        result["role_models"] = _role_models(result["role_models"], old_label)
    return result


def save_user_settings(updates: dict[str, Any], *, validate_only: bool = False) -> dict[str, Any]:
    """Import a patch once and atomically persist the unified role settings."""

    forbidden = {key for key in updates if "key" in key.lower() or "secret" in key.lower()}
    if forbidden:
        raise ConfigurationError("API Key 或其他密钥不能写入设置文件。")
    updates = adapt_settings(updates)
    # 设置页、聊天及MCP手调容量均应保留作者值；明确选择预设时仍允许恢复自动估算。
    if {"agent_context_budgets", "context_soft_tokens", "context_hard_tokens"} & updates.keys():
        updates.setdefault("quality_custom_context", True)
        if "agent_context_budgets" in updates:
            updates.setdefault("context_budget_mode", "custom")
    unknown = set(updates) - PERSISTED_SETTING_NAMES
    if unknown:
        raise ConfigurationError(f"不支持的设置项：{', '.join(sorted(unknown))}")
    with _SETTINGS_WRITE_LOCK:
        current = Settings.from_mapping(load_user_settings()).to_mapping()
        # 自定义改变总容量时按现有角色比例缩放；显式逐角色设置优先，不重置作者的手工比例。
        if "agent_context_budgets" not in updates:
            for bound, field in (("soft", "context_soft_tokens"), ("hard", "context_hard_tokens")):
                if field in updates and type(updates[field]) is int and updates[field] > 0:
                    for budget in current["agent_context_budgets"].values():
                        budget[bound] = max(1, round(budget[bound] * updates[field] / current[field]))
                        if bound == "hard":
                            budget["soft"] = min(budget["soft"], budget["hard"])
            if "context_hard_tokens" in updates and "context_soft_tokens" not in updates:
                current["context_soft_tokens"] = min(current["context_soft_tokens"], _positive_int(updates["context_hard_tokens"], "最大上下文容量"))
        for key, value in updates.items():
            if key in {"agent_generation", "agent_context_budgets", "agent_thinking"}:
                for role, fields in value.items():
                    current[key].setdefault(role, {}).update(fields)
                    if key == "agent_context_budgets" and "hard" in fields and "soft" not in fields:
                        current[key][role]["soft"] = min(current[key][role]["soft"], _positive_int(fields["hard"], "最大上下文容量"))
            elif key == "role_models":
                current[key].update(value)
            else:
                current[key] = value
        validated = Settings.from_mapping(current)
        problems = [problem for role in validated.agent_generation for problem in validated.generation_problems(role)]
        if problems:
            raise ConfigurationError("模型参数尚未保存：" + "；".join(problems) + "。原有设置与密钥保留。")
        if validated.provider_kind == "mimo":
            validated = replace(validated, base_url=mimo_endpoint_for_key(
                validated.base_url, validated.get_api_key() or ""))
        clean = validated.to_mapping()
        if validate_only:
            return clean
        path = user_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(clean, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return validated.to_mapping()


@dataclass(slots=True)
class Settings:
    """运行时配置；敏感字段永远不序列化到项目或用户设置文件。"""

    provider_kind: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    role_models: dict[str, str] = field(default_factory=dict)
    manual_edit_review_enabled: bool = True
    reasoning_effort: str = "high"
    openai_recommended_reasoning: bool = False
    disable_model_reasoning: bool = False
    thinking_policy_version: int = CURRENT_THINKING_POLICY_VERSION
    mimo_retry_policy_version: int = 2
    retrieval_policy_version: int = 3
    # v4在v3请求排列上增加作者界面的公开措辞约束，不改变资料选择和角色权限。
    # v5压缩逐字重复的协调要求，仅将MiMo规划Writer输出格式放到资料末尾，保留原任务合同。
    # v12把已选设定的固定定义、作者硬约定与任务细节分层，只影响新任务的发送投影。
    # 版本冻结到任务，旧任务恢复时不能被当前默认值改写。
    # v15区分章节保护范围与回读范围，避免把“不动前文”扩大为整卷全文取材。
    # v16按当前章节卡单独加权召回伏笔依据，不改旧任务的资料身份。
    # v17角色持续会话跨任务保留闭合回合，接近模型输入窗口时才收拢旧过程。
    # v18首轮原文前置、索引后置；动态结构化资料只发送可逐字重建的标准JSON补丁。
    # v19在90%压力后等待自然边界整理；完整输入检查点与结果校验分开，补缺仅返回错误路径。
    # v20统一输入容量，保护写作承接原文，关键动态资料完整发送，并按失效资料占用整理历史。
    prompt_cache_layout_version: int = 28
    agent_thinking: dict[str, dict[str, str]] = field(default_factory=dict)
    context_soft_tokens: int = 256_000
    context_hard_tokens: int = 614_400
    context_input_limit_tokens: int = SOFTWARE_INPUT_TOKEN_LIMIT
    context_budget_mode: str = "custom"
    agent_context_budgets: dict[str, dict[str, int]] = field(
        default_factory=lambda: {name: {bound: round(tokens * 1.2) if bound == "hard" else tokens for bound, tokens in values.items()}
                                for name, values in DEFAULT_AGENT_CONTEXT_BUDGETS.items()}
    )
    max_output_tokens: int = 32_000
    automatic_output_tokens: bool = False
    max_concurrent_model_requests: int = 3
    writer_task_attempt_limit: int | None = None
    request_timeout_seconds: float = 180.0
    planning_timeout_seconds: float = 600.0
    trace_level: str = "full"
    show_provider_reasoning: bool = True
    inquiry_frequency: str = "medium"
    hook_strategy: str = "most_chapters"
    quality_preset: str = "custom"
    quality_custom_context: bool = True
    collaboration_preset: str = "everyday"
    chapter_length_tolerance: float = 0.10
    review_min_confidence: float = 0.80
    acceptance_confirmation_mode: str = "auto_after_review"
    planning_publication_mode: str = "auto_after_review"
    planning_window_chapters: int = 10
    dialogue_history_mode: str = "auto"
    dialogue_history_interval: int = 1
    dialogue_history_limit: int = 100
    agent_generation: dict[str, dict[str, float | int | None]] = field(
        default_factory=lambda: {name: dict(values) for name, values in DEFAULT_AGENT_GENERATION.items()}
    )
    review_verification_mode: str = "evidence"
    review_experience_detail: str = "standard"
    review_local_nli_model: str = ""
    review_judge_model: str = ""
    retrieval_embedding_model: str = ""
    retrieval_reranker_model: str = ""
    powershell_enabled: bool = False
    voice_enabled: bool = False
    voice_input_enabled: bool = True
    voice_output_enabled: bool = True
    voice_auto_read: bool = False
    voice_auto_send: bool = False
    voice_default_profile: str = "narrator_female"
    voice_speed: float = 1.0
    voice_volume: float = 1.0
    voice_pause_scale: float = 1.0
    voice_input_device: str = ""
    voice_output_device: str = ""
    voice_compute_device: str = "auto"
    voice_input_engine: str = "off"
    voice_dialogue_engine: str = "edge"
    voice_text_engine: str = "edge"
    voice_novel_engine: str = "edge"
    voice_light_asr_model: str = ""
    voice_light_tts_model: str = "MOSS-TTS-Nano-100M-ONNX"
    voice_sample_rate: int = 24000
    voice_segment_chars: int = 360
    voice_cache_limit_mb: int = 1024
    voice_debug: bool = False
    voice_data_dir: str = ""
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    workspace_root: Path | None = None
    role_settings_warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.collaboration_preset not in {"everyday", "adaptive", "full_specialist"}:
            raise ConfigurationError("协作方式请选择日常协作、按需加强或全面协作。")
        if self.quality_preset not in {"economy", "balanced", "quality", "custom"}:
            raise ConfigurationError("质量与消耗请选择节省、平衡、高质量或自定义。")
        self.role_models = _role_models(self.role_models, None)
        self.agent_generation = _agent_generation(self.agent_generation)
        self.agent_context_budgets = _agent_context_budgets(self.agent_context_budgets)
        self.agent_thinking = _agent_thinking(self.agent_thinking)

    @property
    def role_settings_version(self) -> int:
        """Read-only compatibility for obsolete call signatures; never persisted."""
        return ROLE_PROTOCOL_VERSION

    def model_for(self, role: str | None, protocol_version: int = 1) -> str:
        if role is None:
            return self.model
        return self.role_models.get(_settings_role(role, protocol_version), "") or self.model

    def generation_for(self, role: str, protocol_version: int = 1) -> dict[str, float | int | None]:
        canonical = _settings_role(role, protocol_version)
        return dict(self.agent_generation[canonical])

    def model_limits(self, model_override: str | None = None) -> dict[str, Any]:
        return _official_model_limits(self.base_url, model_override or self.model)

    def parameter_capabilities(self, model_override: str | None = None) -> dict[str, Any]:
        """Project the adapter's supported parameters for the actual selected model."""
        model = (model_override or self.model).casefold()
        kind = self.provider_kind
        sampling = {"temperature": True, "top_p": True, "top_k": False}
        constraints: dict[str, Any] = {"temperature": [0, 2], "top_p": [0, 1]}
        effort_values: list[str] = []
        effort_map: dict[str, str] = {}
        non_thinking_effort = "none"
        sampling_without_reasoning_only = False
        completion_tokens = self.is_mimo
        if self.is_mimo:
            constraints.update(temperature=[0, 1.5], top_p=[.01, 1], thinking_temperature=1.0, thinking_top_p=.95)
        elif kind == "anthropic":
            constraints["temperature"] = [0, 1]
            sampling["top_k"] = True
            # New Claude generations reject custom sampling; retain stored values for older models.
            if re.match(r"^claude-(?:(?:sonnet|opus|haiku)-5|opus-4-[78]|mythos|fable)", model):
                sampling = dict.fromkeys(sampling, False)
            elif model.startswith("claude-opus-4-1"):
                sampling["top_p"] = False
        elif self.is_deepseek:
            effort_values = ["low", "high", "max"]
            effort_map = {"medium": "high"}
            if urlparse(self.base_url).hostname == "api.deepseek.com" and model in {
                "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro",
            }:
                constraints.update(thinking_temperature_ignored=True, thinking_top_p_min=.95, non_thinking_top_p=1.0)
        elif kind == "openai":
            reasoning_model = bool(re.match(r"^(?:gpt-[56](?:[.\-]|$)|o[134](?:\-|$))", model)) and "-chat" not in model
            if reasoning_model:
                completion_tokens = True
                modern = bool(re.match(r"^gpt-(?:5\.[1-9]|6)", model))
                supports_none = modern and not model.startswith(("gpt-6-astra", "gpt-6.1-sol"))
                effort_values = (["none"] if supports_none else []) + ["low", "medium", "high"]
                supports_max = bool(re.match(r"^gpt-(?:5\.[6-9]|6)", model))
                if supports_max:
                    effort_values.append("max")
                else:
                    effort_map["max"] = "high"
                non_thinking_effort = "none" if supports_none else "minimal" if model.startswith("gpt-5") and not modern else "low"
                sampling_without_reasoning_only = supports_none
                if modern and not supports_none:
                    sampling.update(temperature=False, top_p=False)
                if not modern:
                    sampling.update(temperature=False, top_p=False)
            if model.startswith(("o1-mini", "o1-preview")):
                effort_values = []
        elif kind == "gemini" and re.match(r"^gemini-(?:2\.5|3|4)", model):
            effort_values = ["low", "medium", "high"]
            effort_map = {"max": "high"}
            non_thinking_effort = "none" if model.startswith("gemini-2.5-flash") else "low"
        elif kind == "openrouter":
            sampling["top_k"] = True
            effort_values = ["none", "low", "medium", "high"]
            effort_map = {"max": "high"}
        elif kind == "ollama" and model.startswith("gpt-oss"):
            effort_values = ["none", "low", "medium", "high"]
            effort_map = {"max": "high"}
        return {"sampling_parameters": sampling, "sampling_constraints": constraints,
                "sampling_without_reasoning_only": sampling_without_reasoning_only,
                "top_k": sampling["top_k"], "reasoning_effort": bool(effort_values),
                "reasoning_effort_values": effort_values, "reasoning_effort_map": effort_map,
                "non_thinking_effort": non_thinking_effort, "max_completion_tokens": completion_tokens}

    def generation_problems(self, role: str, model_override: str | None = None) -> list[str]:
        capabilities = self.parameter_capabilities(model_override or self.model_for(role))
        generation = self.generation_for(role)
        label = {"coordinator": "协调", "writer": "创作", "editor": "编辑",
                 "reviewer": "专项审查", "memory_keeper": "记忆整理"}[role]
        problems = []
        for name, title in (("temperature", "温度"), ("top_p", " Top P")):
            low, high = capabilities["sampling_constraints"][name]
            if capabilities["sampling_parameters"][name] and not low <= generation[name] <= high:
                problems.append(f"{label}{title}须在 {low:g}～{high:g} 之间")
        return problems

    def context_selection_budget_for(self, role: str | None, protocol_version: int = 1,
                                     model_override: str | None = None) -> tuple[int, int]:
        """读取常用取材目标及已配置上下文容量；新布局把容量交由实际发送门禁执行。"""
        if role is not None and self.context_budget_mode == "custom":
            selected = self.agent_context_budgets[_settings_role(role, protocol_version)]
            soft, hard = int(selected["soft"]), int(selected["hard"])
        else:
            soft, hard = self.context_soft_tokens, self.context_hard_tokens
        limits = self.model_limits(model_override or self.model_for(role, protocol_version))
        hard = min(hard, limits.get("context_tokens", hard))
        return min(soft, hard), hard

    def context_budget_for(self, role: str | None, protocol_version: int = 1,
                           model_override: str | None = None) -> tuple[int, int]:
        """统一编译、持续会话和发送的输入上界；取材目标不强制装满，旧任务保留原口径。"""
        soft, configured_capacity = self.context_selection_budget_for(role, protocol_version, model_override)
        limits = self.model_limits(model_override or self.model_for(role, protocol_version))
        hard = min(SOFTWARE_INPUT_TOKEN_LIMIT, limits.get("context_tokens", SOFTWARE_INPUT_TOKEN_LIMIT))
        if self.prompt_cache_layout_version >= 20:
            # 为已知模型保留完整输出能力和封装空间，缓存输入仍然占用同一窗口。
            # hard是该角色全部实际输入的容量，包含历史消息与规则，不能只用来选资料。
            hard = min(hard, self.context_input_limit_tokens, configured_capacity)
            if limits.get("context_tokens"):
                hard = min(hard, max(1, limits["context_tokens"] - limits.get("max_output_tokens", 0) - 1024))
        return min(soft, hard), hard

    @property
    def input_token_factor(self) -> float:
        """按实报偏差给新任务留估算余量；不是官方 tokenizer，不改冻结旧任务。"""
        if self.is_mimo:
            return 1.3
        # DeepSeek实报曾比原估算高约11%～13%；20%余量用于所有角色的装载与发送前核算。
        return 1.2 if self.is_deepseek and self.prompt_cache_layout_version >= 11 else 1.0

    @property
    def generation_output_policy(self) -> str:
        """Current application policy; legacy snapshot fields remain unchanged."""
        return "model_capacity" if self.request_output_limit(0) is not None else "provider_default"

    def request_output_limit(self, input_tokens: int, role: str | None = None,
                             model_override: str | None = None) -> int | None:
        """DeepSeek省略参数会落入8K默认；使用模型可用空间，不沿用旧软件输出配额。"""
        limits = self.model_limits(model_override or self.model_for(role))
        if not self.is_deepseek or not limits.get("max_output_tokens"):
            return None
        if type(input_tokens) is not int or input_tokens < 0:
            raise ConfigurationError("本次完整输入的token估算无效，未猜测可用输出空间。")
        remaining = limits["context_tokens"] - input_tokens - 1024
        if remaining <= 0:
            raise ConfigurationError("本次输入已占满模型总窗口，尚无可用输出空间；请先调整输入资料。")
        return min(limits["max_output_tokens"], remaining)

    def output_reservation_upper(self, role: str | None = None, model_override: str | None = None) -> int | None:
        value = self.model_limits(model_override or self.model_for(role)).get("max_output_tokens")
        return value if type(value) is int and value > 0 else None

    def validate_input_budget(self, input_tokens: int, role: str | None = None,
                              model_override: str | None = None, *, hard_rule_tokens: int = 0) -> dict[str, int]:
        """分别核软件一千万上限与模型窗口；硬规则按实际占用计，不虚占空白容量。"""
        _, hard = self.context_budget_for(role, model_override=model_override)
        if type(input_tokens) is not int or input_tokens < 0:
            raise ConfigurationError("完整输入用量须为非负整数，不能用未知值跳过容量核对。")
        if type(hard_rule_tokens) is not int or hard_rule_tokens < 0:
            raise ConfigurationError("硬规则用量须为非负整数，不能将未知值记为实际占用。")
        protected = min(input_tokens, hard_rule_tokens)
        if input_tokens > SOFTWARE_INPUT_TOKEN_LIMIT:
            raise ConfigurationError(f"{role or '本次请求'} 完整输入估算 {input_tokens:,} tokens，"
                f"超过软件输入上限 {SOFTWARE_INPUT_TOKEN_LIMIT:,}；请精简重复或无关材料，保留完整硬规则。")
        if input_tokens > hard:
            raise ConfigurationError(f"{role or '本次请求'} 完整输入估算 {input_tokens:,} tokens，"
                f"超过本次有效输入上限 {hard:,}；该上限受设置及模型窗口约束，并按当前策略保留输出空间。")
        return {"hard_rule_reserved_tokens": protected, "protected_hard_tokens": protected,
                "hard_rule_unfilled_reserve_tokens": 0, "effective_input_ceiling_tokens": hard}

    def estimate_input_tokens(self, text: str) -> int:
        from .utils import estimate_tokens
        return math.ceil(estimate_tokens(text) * self.input_token_factor)

    def input_estimate_budget(self, tokens: int) -> int:
        """Convert available real-token budget to the shared raw estimate's units."""
        return max(0, math.floor(tokens / self.input_token_factor))

    def effective_output_tokens(self, requested: int, role: str | None = None,
                                model_override: str | None = None, input_tokens: int = 0) -> int:
        """Legacy preparation hint only; Providers no longer send this as a cap."""
        requested = _positive_int(requested, "本次请求输出额度")
        selected = model_override or self.model_for(role)
        _, hard = self.context_budget_for(role, model_override=selected)
        remaining = hard - max(0, int(input_tokens))
        if remaining <= 0:
            raise ConfigurationError(f"{role or '本次请求'} 的模型 {selected} 上下文额度已用尽："
                                     f"完整输入约 {input_tokens:,} tokens，可用上下文 {hard:,} tokens。"
                                     "请调整该角色预算或缩小本次输入后继续。")
        limits = self.model_limits(selected)
        return min(requested, remaining, limits.get("max_output_tokens", requested))

    def role_settings_view(self, protocol_version: int = 1) -> dict[str, Any]:
        return {
            "role_models": dict(self.role_models),
            "agent_generation": {
                role: dict(values) for role, values in self.agent_generation.items()
            },
            "agent_context_budgets": {
                role: dict(values) for role, values in self.agent_context_budgets.items()
            },
        }

    def to_mapping(self, protocol_version: int = ROLE_PROTOCOL_VERSION) -> dict[str, Any]:
        """Serialize canonical settings; the obsolete argument does not select a view."""
        values = {key: value for key, value in asdict(self).items() if key in PERSISTED_SETTING_NAMES}
        # 默认关闭时保留历史快照和旧报告使用的配置指纹。
        if not self.openai_recommended_reasoning:
            values.pop("openai_recommended_reasoning")
        if not self.disable_model_reasoning:
            values.pop("disable_model_reasoning")
        values.update(self.role_settings_view(protocol_version))
        return values

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        workspace_root: str | Path | None = None,
    ) -> "Settings":
        value = adapt_settings(value)
        # Normalize legacy model IDs even when they come from workspace settings
        # or environment variables, rather than only the global settings file.
        for key, previous, current in (
            ("voice_light_tts_model", "sherpa-onnx-vits-zh-ll", "MOSS-TTS-Nano-100M-ONNX"),
            ("voice_light_tts_model", "kokoro-int8-multi-lang-v1_1", "MOSS-TTS-Nano-100M-ONNX"),
            ("voice_light_asr_model", "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", ""),
        ):
            if str(value.get(key, "")).strip() == previous:
                value[key] = current
        defaults = cls()
        provider_kind = str(value.get("provider_kind", defaults.provider_kind)).strip().lower()
        if provider_kind not in PROVIDER_KINDS:
            raise ConfigurationError(f"不支持的模型服务商：{provider_kind}")
        effort = str(value.get("reasoning_effort", defaults.reasoning_effort)).lower()
        if effort not in ALLOWED_REASONING_EFFORTS:
            raise ConfigurationError("思考强度只能是 low、medium、high 或 max。")
        policy_version = value.get("thinking_policy_version", CURRENT_THINKING_POLICY_VERSION)
        if type(policy_version) is not int or not 1 <= policy_version <= CURRENT_THINKING_POLICY_VERSION:
            raise ConfigurationError(f"思考策略版本只能是1至{CURRENT_THINKING_POLICY_VERSION}。")
        retry_policy_version = value.get("mimo_retry_policy_version", 2)
        if type(retry_policy_version) is not int or retry_policy_version not in {1, 2}:
            raise ConfigurationError("MiMo重试策略版本只能是1或2。")
        retrieval_policy_version = value.get("retrieval_policy_version", 3)
        if type(retrieval_policy_version) is not int or retrieval_policy_version not in {1, 2, 3}:
            raise ConfigurationError("检索策略版本只能是1至3。")
        prompt_cache_layout_version = value.get("prompt_cache_layout_version", 28)
        if type(prompt_cache_layout_version) is not int or prompt_cache_layout_version not in range(1, 29):
            raise ConfigurationError("请求缓存布局版本只能是1至28。")
        trace_level = str(value.get("trace_level", defaults.trace_level)).lower()
        if trace_level not in {"compact", "standard", "full"}:
            raise ConfigurationError("过程记录级别只能是 compact、standard 或 full。")
        base_url = str(value.get("base_url", "https://api.xiaomimimo.com/v1" if provider_kind == "mimo" else defaults.base_url)).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("模型接口地址必须是完整的 http 或 https 地址。")
        model = str(value.get("model", "mimo-v2.6-flash" if provider_kind == "mimo" else defaults.model)).strip()
        if not model:
            raise ConfigurationError("模型名称不能为空。")
        soft = _positive_int(value.get("context_soft_tokens", defaults.context_soft_tokens if prompt_cache_layout_version >= 20 else 256_000), "常用取材量")
        hard = _positive_int(value.get("context_hard_tokens", defaults.context_hard_tokens if prompt_cache_layout_version >= 20 else 512_000), "最大上下文容量")
        raw_input_limit = value.get("context_input_limit_tokens", SOFTWARE_INPUT_TOKEN_LIMIT)
        if type(raw_input_limit) not in {int, str}:
            raise ConfigurationError("最大输入量必须是整数，不能使用开关或小数。")
        input_limit = _bounded_int_range(raw_input_limit, "最大输入量", 1, SOFTWARE_INPUT_TOKEN_LIMIT)
        output = _positive_int(value.get("max_output_tokens", defaults.max_output_tokens), "单次输出上限")
        automatic_output = _as_bool(value.get("automatic_output_tokens", False))
        max_concurrent_model_requests = _bounded_int_range(
            value.get("max_concurrent_model_requests", defaults.max_concurrent_model_requests),
            "模型请求并发上限",
            1,
            16,
        )
        writer_task_attempt_limit = value.get("writer_task_attempt_limit", defaults.writer_task_attempt_limit)
        if writer_task_attempt_limit is not None:
            if isinstance(writer_task_attempt_limit, bool) or not isinstance(writer_task_attempt_limit, (int, str)):
                raise ConfigurationError("本任务写作请求上限必须是正整数或留空。")
            writer_task_attempt_limit = _positive_int(writer_task_attempt_limit, "本任务写作请求上限")
        if soft > hard:
            raise ConfigurationError("软上下文预算不能大于硬上下文上限。")
        context_budget_mode = _choice(
            value.get("context_budget_mode", defaults.context_budget_mode if prompt_cache_layout_version >= 20 else "unified"),
            "上下文预算模式",
            {"unified", "custom"},
        )
        raw_agent_budgets, context_warnings = _migrate_settings_roles(
            value.get("agent_context_budgets", {}), None
        )
        agent_context_budgets = _agent_context_budgets(raw_agent_budgets,
            capacity_scale=1.2 if prompt_cache_layout_version >= 20 else 1.0,
            strict=prompt_cache_layout_version >= 20)
        if prompt_cache_layout_version >= 20 and any(budget["hard"] > SOFTWARE_INPUT_TOKEN_LIMIT for budget in agent_context_budgets.values()):
            raise ConfigurationError("各角色最大上下文容量不能超过软件一千万tokens上限。")
        # Preserve old values for snapshot identity. Current provider-default
        # execution does not send these legacy per-request output caps.
        timeout = _positive_float(
            value.get("request_timeout_seconds", defaults.request_timeout_seconds), "普通请求超时"
        )
        planning_timeout = _positive_float(
            value.get("planning_timeout_seconds", defaults.planning_timeout_seconds), "规划请求超时"
        )
        inquiry_frequency = str(value.get("inquiry_frequency", defaults.inquiry_frequency)).lower()
        if inquiry_frequency not in {"low", "medium", "high", "ultra"}:
            raise ConfigurationError("主动询问频率只能是 low、medium、high 或 ultra。")
        hook_strategy = _choice(
            value.get("hook_strategy", defaults.hook_strategy),
            "章节结尾策略",
            {"most_chapters", "key_chapters", "natural_afterglow"},
        )
        chapter_length_tolerance = _bounded_float(
            value.get("chapter_length_tolerance", defaults.chapter_length_tolerance),
            "章节长度容差",
            0.05,
            0.30,
        )
        review_min_confidence = _bounded_float(
            value.get("review_min_confidence", defaults.review_min_confidence),
            "审查最低把握度",
            0.70,
            1.00,
        )
        acceptance_confirmation_mode = _choice(
            value.get("acceptance_confirmation_mode", defaults.acceptance_confirmation_mode),
            "验收确认策略",
            {"per_chapter", "batch_once", "auto_after_review"},
        )
        planning_publication_mode = _choice(
            value.get("planning_publication_mode", defaults.planning_publication_mode),
            "规划正式发布方式", {"auto_after_review", "confirm_after_review"},
        )
        planning_window_chapters = _bounded_int_range(
            value.get("planning_window_chapters", defaults.planning_window_chapters),
            "默认近期规划章数", 1, 50,
        )
        dialogue_history_mode = _choice(
            value.get("dialogue_history_mode", defaults.dialogue_history_mode),
            "对话历史保存方式",
            {"manual", "auto", "both"},
        )
        dialogue_history_interval = _bounded_int_range(
            value.get("dialogue_history_interval", defaults.dialogue_history_interval),
            "对话历史自动保存间隔",
            1,
            50,
        )
        dialogue_history_limit = _bounded_int_range(
            value.get("dialogue_history_limit", defaults.dialogue_history_limit),
            "对话历史保留上限",
            5,
            1000,
        )
        raw_generation, generation_warnings = _migrate_settings_roles(
            value.get("agent_generation", {}), None
        )
        agent_generation = _agent_generation(raw_generation)
        review_verification_mode = str(
            value.get("review_verification_mode", defaults.review_verification_mode)
        ).lower()
        if review_verification_mode not in {"evidence", "assisted", "strict"}:
            raise ConfigurationError("审核核验模式只能是 evidence、assisted 或 strict。")
        review_experience_detail = _choice(
            value.get("review_experience_detail", defaults.review_experience_detail),
            "阅读体验建议详细度",
            {"concise", "standard", "detailed"},
        )
        return cls(
            manual_edit_review_enabled=_as_bool(value.get("manual_edit_review_enabled", True)),
            role_models=_role_models(value.get("role_models", {}), None),
            provider_kind=provider_kind,
            base_url=base_url,
            model=model,
            reasoning_effort=effort,
            openai_recommended_reasoning=_as_bool(value.get("openai_recommended_reasoning", False)),
            disable_model_reasoning=_as_bool(value.get("disable_model_reasoning", False)),
            thinking_policy_version=policy_version,
            mimo_retry_policy_version=retry_policy_version,
            retrieval_policy_version=retrieval_policy_version,
            prompt_cache_layout_version=prompt_cache_layout_version,
            agent_thinking=_agent_thinking(value.get("agent_thinking", {})),
            context_soft_tokens=soft,
            context_hard_tokens=hard,
            context_input_limit_tokens=input_limit,
            context_budget_mode=context_budget_mode,
            agent_context_budgets=agent_context_budgets,
            role_settings_warnings=tuple(dict.fromkeys((*context_warnings, *generation_warnings))),
            max_output_tokens=output,
            automatic_output_tokens=automatic_output,
            max_concurrent_model_requests=max_concurrent_model_requests,
            writer_task_attempt_limit=writer_task_attempt_limit,
            request_timeout_seconds=timeout,
            planning_timeout_seconds=planning_timeout,
            trace_level=trace_level,
            show_provider_reasoning=_as_bool(
                value.get("show_provider_reasoning", defaults.show_provider_reasoning)
            ),
            inquiry_frequency=inquiry_frequency,
            hook_strategy=hook_strategy,
            quality_preset=str(value.get("quality_preset", defaults.quality_preset)),
            quality_custom_context=_as_bool(value.get("quality_custom_context", defaults.quality_custom_context)),
            collaboration_preset=str(value.get("collaboration_preset", "everyday")),
            chapter_length_tolerance=chapter_length_tolerance,
            review_min_confidence=review_min_confidence,
            acceptance_confirmation_mode=acceptance_confirmation_mode,
            planning_publication_mode=planning_publication_mode,
            planning_window_chapters=planning_window_chapters,
            dialogue_history_mode=dialogue_history_mode,
            dialogue_history_interval=dialogue_history_interval,
            dialogue_history_limit=dialogue_history_limit,
            agent_generation=agent_generation,
            review_verification_mode=review_verification_mode,
            review_experience_detail=review_experience_detail,
            review_local_nli_model=str(
                value.get("review_local_nli_model", defaults.review_local_nli_model)
            ).strip(),
            review_judge_model=str(value.get("review_judge_model", defaults.review_judge_model)).strip(),
            retrieval_embedding_model=str(
                value.get("retrieval_embedding_model", defaults.retrieval_embedding_model)
            ).strip(),
            retrieval_reranker_model=str(
                value.get("retrieval_reranker_model", defaults.retrieval_reranker_model)
            ).strip(),
            powershell_enabled=_as_bool(value.get("powershell_enabled", defaults.powershell_enabled)),
            voice_enabled=_as_bool(value.get("voice_enabled", defaults.voice_enabled)),
            voice_input_enabled=_as_bool(value.get("voice_input_enabled", defaults.voice_input_enabled)),
            voice_output_enabled=_as_bool(value.get("voice_output_enabled", defaults.voice_output_enabled)),
            voice_auto_read=_as_bool(value.get("voice_auto_read", defaults.voice_auto_read)),
            voice_auto_send=_as_bool(value.get("voice_auto_send", defaults.voice_auto_send)),
            voice_default_profile=str(value.get("voice_default_profile", defaults.voice_default_profile)).strip()
            or defaults.voice_default_profile,
            voice_speed=_bounded_float(value.get("voice_speed", defaults.voice_speed), "语音速度", 0.75, 1.35),
            voice_volume=_bounded_float(value.get("voice_volume", defaults.voice_volume), "语音音量", 0.25, 1.5),
            voice_pause_scale=_bounded_float(
                value.get("voice_pause_scale", defaults.voice_pause_scale),
                "语音停顿强度",
                0.6,
                1.8,
            ),
            voice_input_device=str(value.get("voice_input_device", defaults.voice_input_device)).strip(),
            voice_output_device=str(value.get("voice_output_device", defaults.voice_output_device)).strip(),
            voice_compute_device=_choice(
                value.get("voice_compute_device", defaults.voice_compute_device),
                "语音计算设备",
                {"auto", "cpu", "cuda"},
            ),
            # ??? auto / sherpa / Kokoro ????????? MOSS?
            voice_input_engine=_choice(value.get("voice_input_engine", defaults.voice_input_engine), "语音输入引擎", {"off", "local", "browser"}),
            voice_dialogue_engine=_choice("off" if value.get("voice_dialogue_engine") == "moss" else value.get("voice_dialogue_engine", defaults.voice_dialogue_engine), "对话朗读", {"off", "edge"}),
            voice_text_engine=_choice("off" if value.get("voice_text_engine") == "moss" else value.get("voice_text_engine", defaults.voice_text_engine), "文本朗读", {"off", "edge"}),
            voice_novel_engine=_choice("off" if value.get("voice_novel_engine") == "moss" else value.get("voice_novel_engine", defaults.voice_novel_engine), "正文朗读", {"off", "edge"}),
            voice_light_asr_model=str(value.get("voice_light_asr_model", defaults.voice_light_asr_model)).strip()
            or defaults.voice_light_asr_model,
            voice_light_tts_model=str(value.get("voice_light_tts_model", defaults.voice_light_tts_model)).strip()
            or defaults.voice_light_tts_model,
            voice_sample_rate=_bounded_int(
                value.get("voice_sample_rate", defaults.voice_sample_rate),
                "语音采样率",
                {16_000, 22_050, 24_000, 44_100, 48_000},
            ),
            voice_segment_chars=_bounded_int_range(
                value.get("voice_segment_chars", defaults.voice_segment_chars),
                "语音分段字数",
                120,
                800,
            ),
            voice_cache_limit_mb=_bounded_int_range(
                value.get("voice_cache_limit_mb", defaults.voice_cache_limit_mb),
                "语音缓存上限",
                128,
                8192,
            ),
            voice_debug=_as_bool(value.get("voice_debug", defaults.voice_debug)),
            voice_data_dir=str(value.get("voice_data_dir", defaults.voice_data_dir) or "").strip(),
            input_price_per_million=_non_negative_float(
                value.get("input_price_per_million", defaults.input_price_per_million), "输入单价"
            ),
            output_price_per_million=_non_negative_float(
                value.get("output_price_per_million", defaults.output_price_per_million), "输出单价"
            ),
            workspace_root=Path(workspace_root).resolve() if workspace_root else None,
        )

    @classmethod
    def from_env(cls, workspace_root: str | Path | None = None) -> "Settings":
        """载入当前工作区设置；原任务另由from_mapping按冻结版本恢复。"""
        value = load_user_settings()
        policy_version = value.get("thinking_policy_version")
        if type(policy_version) is int and 1 <= policy_version < CURRENT_THINKING_POLICY_VERSION:
            value["thinking_policy_version"] = CURRENT_THINKING_POLICY_VERSION
        # 只升级新任务；已付费任务仍从原快照恢复原取材策略，不能冒用新输入复用旧报告。
        if type(value.get("retrieval_policy_version", 3)) is int and value.get("retrieval_policy_version") in {1, 2}:
            value = {**value, "retrieval_policy_version": 3}
        # 旧全局设置曾随保存携带内部布局版本；只升级新任务的内存默认，不改原文件或快照。
        # 未知值、布尔值等仍交from_mapping报错，不能借升级掩盖配置损坏。
        layout_version = value.get("prompt_cache_layout_version", 28)
        if type(layout_version) is int and layout_version in range(1, 28):
            value = {**value, "prompt_cache_layout_version": 28}
        # 新任务回到作者的角色范围与档位；旧快照仍按记录中的配置恢复。
        value.pop("openai_recommended_reasoning", None)
        value.pop("disable_model_reasoning", None)
        environment_mapping = {
            "provider_kind": "INKFLOW_PROVIDER",
            "base_url": "INKFLOW_BASE_URL",
            "model": "INKFLOW_MODEL",
            "reasoning_effort": "INKFLOW_REASONING_EFFORT",
            "context_soft_tokens": "INKFLOW_CONTEXT_SOFT_TOKENS",
            "context_hard_tokens": "INKFLOW_CONTEXT_HARD_TOKENS",
            "context_input_limit_tokens": "INKFLOW_CONTEXT_INPUT_LIMIT_TOKENS",
            "max_output_tokens": "INKFLOW_MAX_OUTPUT_TOKENS",
            "max_concurrent_model_requests": "INKFLOW_MAX_CONCURRENT_MODEL_REQUESTS",
            "writer_task_attempt_limit": "INKFLOW_WRITER_TASK_ATTEMPT_LIMIT",
            "request_timeout_seconds": "INKFLOW_TIMEOUT_SECONDS",
            "planning_timeout_seconds": "INKFLOW_PLANNING_TIMEOUT_SECONDS",
            "trace_level": "INKFLOW_TRACE_LEVEL",
            "show_provider_reasoning": "INKFLOW_SHOW_REASONING",
            "inquiry_frequency": "INKFLOW_INQUIRY_FREQUENCY",
            "hook_strategy": "INKFLOW_HOOK_STRATEGY",
            "chapter_length_tolerance": "INKFLOW_CHAPTER_LENGTH_TOLERANCE",
            "review_min_confidence": "INKFLOW_REVIEW_MIN_CONFIDENCE",
            "acceptance_confirmation_mode": "INKFLOW_ACCEPTANCE_CONFIRMATION_MODE",
            "planning_publication_mode": "INKFLOW_PLANNING_PUBLICATION_MODE",
            "planning_window_chapters": "INKFLOW_PLANNING_WINDOW_CHAPTERS",
            "review_verification_mode": "INKFLOW_REVIEW_VERIFICATION_MODE",
            "review_experience_detail": "INKFLOW_REVIEW_EXPERIENCE_DETAIL",
            "review_local_nli_model": "INKFLOW_REVIEW_LOCAL_NLI_MODEL",
            "review_judge_model": "INKFLOW_REVIEW_JUDGE_MODEL",
            "retrieval_embedding_model": "INKFLOW_RETRIEVAL_EMBEDDING_MODEL",
            "retrieval_reranker_model": "INKFLOW_RETRIEVAL_RERANKER_MODEL",
            "powershell_enabled": "INKFLOW_POWERSHELL_ENABLED",
            "voice_enabled": "INKFLOW_VOICE_ENABLED",
            "voice_compute_device": "INKFLOW_VOICE_DEVICE",
            "voice_input_engine": "INKFLOW_VOICE_INPUT_ENGINE",
            "voice_dialogue_engine": "INKFLOW_VOICE_DIALOGUE_ENGINE",
            "voice_text_engine": "INKFLOW_VOICE_TEXT_ENGINE",
            "voice_novel_engine": "INKFLOW_VOICE_NOVEL_ENGINE",
            "voice_light_asr_model": "INKFLOW_VOICE_LIGHT_ASR_MODEL",
            "voice_light_tts_model": "INKFLOW_VOICE_LIGHT_TTS_MODEL",
            "input_price_per_million": "INKFLOW_INPUT_PRICE_PER_MILLION",
            "output_price_per_million": "INKFLOW_OUTPUT_PRICE_PER_MILLION",
        }
        for field, environment_name in environment_mapping.items():
            if environment_name in os.environ:
                value[field] = os.environ[environment_name]
        settings = cls.from_mapping(value, workspace_root)
        if settings.provider_kind == "mimo":
            # Only live settings follow the current credential; frozen task mappings stay unchanged.
            settings = replace(settings, base_url=mimo_endpoint_for_key(
                settings.base_url, settings.get_api_key() or ""))
        return settings

    @property
    def is_deepseek(self) -> bool:
        return self.provider_kind == "deepseek" or "deepseek" in urlparse(self.base_url).netloc.casefold()

    @property
    def is_mimo(self) -> bool:
        return self.provider_kind == "mimo" or urlparse(self.base_url).hostname in {
            "api.xiaomimimo.com", "token-plan-cn.xiaomimimo.com",
        }

    @property
    def prompt_cache_policy(self) -> str:
        """官方 OpenAI 新任务独立采用稳定资料前缀，兼容接口沿用原布局。"""
        if self.is_mimo or self.is_deepseek:
            return "mimo" if self.is_mimo else "deepseek"
        if (self.prompt_cache_layout_version >= 28 and self.provider_kind == "openai"
                and urlparse(self.base_url).hostname == "api.openai.com"):
            return "openai"
        return "standard"

    @property
    def model_attempt_limit(self) -> int:
        """补缺处理次数与质量档解耦；显式单次授权及原任务预算仍可进一步收紧。"""
        from .runtime import COMPLETION_ATTEMPT_LIMIT
        return COMPLETION_ATTEMPT_LIMIT

    @property
    def network_attempt_limit(self) -> int:
        """网络与格式共用同一次交付的十次上限，不因供应商或质量档再乘次数。"""
        return self.model_attempt_limit

    def thinking_for(self, role: str | None, requested: bool = True, *,
                     purpose: str = "routine", instruction: str = "") -> bool:
        """按当次指令和角色覆盖范围决定是否思考，旧快照仍支持全局关闭。"""
        if self.thinking_policy_version <= 6 and self.disable_model_reasoning:
            return False
        if self.thinking_policy_version == 1:
            if not (self.is_mimo or self.is_deepseek):
                return requested
            if role in {"writer", "coordinator"}:
                return self.quality_preset != "economy"
            return True if role in {"editor", "reviewer", "memory_keeper"} else requested
        # Only caller-bound user instructions are inspected, never retrieved prose or model output.
        if re.search(r"不要\s*(?:关闭|禁用)\s*(?:深度)?(?:思考|推理)", instruction):
            return True
        if re.search(r"(?:关闭|禁用|不要|无需|不使用)\s*(?:开启|启用|使用)?\s*(?:深度)?(?:思考|推理)(?:模式)?", instruction):
            return False
        if re.search(r"(?:开启|启用|使用|请|需要)\s*(?:深度)?(?:思考|推理)(?:模式)?", instruction):
            return True
        if self.quality_custom_context or self.quality_preset == "custom":
            coverage = self.agent_thinking.get(role, {}).get("coverage", "default")
            if coverage != "default":
                return coverage == "all" or (coverage == "complex" and purpose in {
                    "causality", "timeline", "payoff", "planning_conflict"})
        if self.thinking_policy_version >= 4 and role in DEFAULT_AGENT_GENERATION:
            # 新任务采用作者逐角色配置；复杂任务不暗中重开节省档的调度/写作思考。
            return preset_agent_thinking(self.quality_preset if self.quality_preset != "custom" else "balanced", self.thinking_policy_version)[role]["coverage"] != "off"
        if self.quality_preset == "quality":
            return True
        if self.thinking_policy_version >= 3 and self.quality_preset == "economy":
            return purpose == "planning_conflict" or role in {"editor", "reviewer"} and purpose in {"causality", "timeline"}
        return purpose in {"causality", "timeline", "payoff", "planning_conflict"}

    def thinking_effort(self, requested: str | None = None, *, role: str | None = None,
                        model_override: str | None = None) -> str:
        """Bound new policy effort by tier; adapters still map actual model capabilities."""
        if self.quality_custom_context or self.quality_preset == "custom":
            effort = self.agent_thinking.get(role, {}).get("effort", "default")
            if effort != "default":
                return effort
        if self.thinking_policy_version == 1:
            return requested or self.reasoning_effort
        if self.thinking_policy_version >= 4 and role in DEFAULT_AGENT_GENERATION:
            effort = preset_agent_thinking(self.quality_preset if self.quality_preset != "custom" else "balanced",
                                           self.thinking_policy_version)[role]["effort"]
            # 只提高 OpenAI 核对角色的默认强度，沿用原思考覆盖和作者明确设定。
            if (self.openai_recommended_reasoning and self.provider_kind == "openai"
                    and not (self.is_deepseek or self.is_mimo)
                    and role in {"editor", "reviewer", "memory_keeper"}):
                raised = {"low": "medium", "medium": "high"}.get(effort)
                if raised in self.parameter_capabilities(model_override or self.model_for(role))["reasoning_effort_values"]:
                    return raised
            return effort
        if self.quality_preset == "quality":
            return "high"
        if self.quality_preset == "economy":
            if self.thinking_policy_version >= 3:
                return "low"
            return "low" if (requested or self.reasoning_effort) == "low" else "medium"
        return "medium"

    def generation_output_budget(self, role: str | None, requested: int, thinking: bool) -> int:
        # Purpose hints are not actual request limits. Old saved cap/thinking
        # preferences cannot reinstate an output cap under current policy.
        return requested

    def get_api_key(self) -> str | None:
        provider_env = PROVIDER_ENV_KEYS.get(self.provider_kind)
        for name in tuple(name for name in ("INKFLOW_API_KEY", provider_env) if name):
            value = os.getenv(name)
            if value:
                return value.strip()
        try:
            import keyring

            stored = keyring.get_password(KEYRING_SERVICE, _keyring_account(self.provider_kind))
            if not stored and self.provider_kind == "deepseek":
                stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            return stored.strip() if stored else None
        except Exception:
            return None

    def require_api_key(self) -> str:
        if self.provider_kind == "ollama":
            return self.get_api_key() or ""
        key = self.get_api_key()
        if not key:
            environment_name = PROVIDER_ENV_KEYS.get(self.provider_kind, "INKFLOW_API_KEY")
            raise ConfigurationError(
                "未找到模型 API Key。请在墨流设置中保存到 Windows 凭据库，"
                f"或设置 INKFLOW_API_KEY / {environment_name} 环境变量。"
            )
        host = urlparse(self.base_url).hostname
        if host == "api.xiaomimimo.com" and key.startswith(("tp-", "ttp-")):
            raise ConfigurationError("MiMo Token Plan 密钥需要订阅专用地址 https://token-plan-cn.xiaomimimo.com/v1；尚未发送请求。")
        if host == "token-plan-cn.xiaomimimo.com" and key.startswith("sk-"):
            raise ConfigurationError("MiMo Token Plan 地址需要专用订阅密钥；普通按量密钥请配合 https://api.xiaomimimo.com/v1，尚未发送请求。")
        return key


def api_key_status(provider_kind: str = "deepseek") -> dict[str, Any]:
    """只返回来源和是否存在，不读取到调用者，更不返回密钥本身。"""

    provider_kind = provider_kind.strip().lower()
    def present(key: str, storage: str) -> dict[str, Any]:
        return {"api_key_configured": bool(key), "api_key_storage": storage,
                "api_key_preview": ("••••••••" + key[-4:]) if key else ""}
    if os.getenv("INKFLOW_API_KEY"):
        return present(os.environ["INKFLOW_API_KEY"].strip(), "environment:INKFLOW_API_KEY")
    environment_name = PROVIDER_ENV_KEYS.get(provider_kind)
    if environment_name and os.getenv(environment_name):
        return present(os.environ[environment_name].strip(), f"environment:{environment_name}")
    if provider_kind == "ollama":
        return {"api_key_configured": True, "api_key_storage": "not_required"}
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, _keyring_account(provider_kind)) or ""
        if not stored and provider_kind == "deepseek":
            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or ""
    except Exception:
        stored = ""
    return present(stored, "windows_credential_manager" if stored else "not_configured")


def mimo_endpoint_for_key(base_url: str, key: str) -> str:
    if base_url.strip().rstrip("/") not in {
        "https://api.xiaomimimo.com/v1", "https://token-plan-cn.xiaomimimo.com/v1",
    }:
        return base_url
    if key.startswith(("tp-", "ttp-")):
        return "https://token-plan-cn.xiaomimimo.com/v1"
    if key.startswith("sk-"):
        return "https://api.xiaomimimo.com/v1"
    return base_url


def save_api_key_to_keyring(api_key: str, provider_kind: str = "deepseek") -> None:
    if not api_key.strip():
        raise ConfigurationError("API Key 不能为空。")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _keyring_account(provider_kind), api_key.strip())
    except Exception as exc:
        raise ConfigurationError(f"无法写入系统凭据库：{exc}") from exc


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是整数。") from exc
    if result <= 0:
        raise ConfigurationError(f"{label}必须大于零。")
    return result


def _bounded_int(value: Any, label: str, choices: set[int]) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是整数。") from exc
    if result not in choices:
        raise ConfigurationError(f"{label}只支持：{', '.join(str(item) for item in sorted(choices))}。")
    return result


def _bounded_int_range(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是整数。") from exc
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return result


def _positive_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是数字。") from exc
    if result <= 0:
        raise ConfigurationError(f"{label}必须大于零。")
    return result


def _non_negative_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是数字。") from exc
    if result < 0:
        raise ConfigurationError(f"{label}不能小于零。")
    return result


def _bounded_float(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是数字。") from exc
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{label}必须在 {minimum} 到 {maximum} 之间。")
    return result


def _choice(value: Any, label: str, choices: set[str]) -> str:
    result = str(value).strip().lower()
    if result not in choices:
        raise ConfigurationError(f"{label}只支持：{', '.join(sorted(choices))}。")
    return result


def _keyring_account(provider_kind: str) -> str:
    provider = provider_kind.strip().lower()
    if provider not in PROVIDER_KINDS:
        raise ConfigurationError(f"不支持的模型服务商：{provider}")
    return f"provider:{provider}:api_key"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off"}


def _settings_role(role: str, protocol_version: int) -> str:
    try:
        return normalize_role(role)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _migrate_settings_roles(value: Any, version: int | None) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    try:
        return migrate_role_settings(value, protocol_version=version)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _agent_thinking(value: Any) -> dict[str, dict[str, str]]:
    """Validate explicit role overrides; absent fields keep the frozen tier policy."""
    if not isinstance(value, dict) or set(value) - DEFAULT_AGENT_GENERATION.keys():
        raise ConfigurationError("角色思考设置必须使用现有五个角色。")
    result = {}
    for role in DEFAULT_AGENT_GENERATION:
        raw = value.get(role, {})
        if not isinstance(raw, dict) or set(raw) - {"effort", "coverage"}:
            raise ConfigurationError(f"{role} 的思考设置仅支持强度与覆盖范围。")
        result[role] = {
            "effort": _choice(raw.get("effort", "default"), f"{role} 思考强度", ALLOWED_REASONING_EFFORTS | {"default"}),
            "coverage": _choice(raw.get("coverage", "default"), f"{role} 思考覆盖", {"default", "all", "complex", "off"}),
        }
    return result


def _agent_context_budgets(value: Any, *, capacity_scale: float = 1.0, strict: bool = False) -> dict[str, dict[str, int]]:
    """核对常用取材量与实际上下文容量；仅缺省最大容量增加20%，明确保存值保留。"""
    if not isinstance(value, dict):
        raise ConfigurationError("各 Agent 上下文预算必须是对象。")
    unknown = set(value) - DEFAULT_ROLE_CONTEXT_BUDGETS.keys()
    if unknown:
        raise ConfigurationError(f"不支持的 Agent 角色：{', '.join(sorted(unknown))}")
    result: dict[str, dict[str, int]] = {}
    for role, fallback in DEFAULT_ROLE_CONTEXT_BUDGETS.items():
        fallback = {bound: round(tokens * capacity_scale) if bound == "hard" else tokens for bound, tokens in fallback.items()}
        raw = value.get(role, fallback)
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{role} 的上下文预算格式无效。")
        extra = set(raw) - {"soft", "hard"}
        if extra:
            raise ConfigurationError(f"{role} 包含不支持的上下文参数：{', '.join(sorted(extra))}")
        if strict and any(type(tokens) not in {int, str} for tokens in raw.values()):
            raise ConfigurationError(f"{role} 取材量必须是整数，不能使用开关或小数。")
        role_soft = _positive_int(raw.get("soft", fallback["soft"]), f"{role} 常用上下文预算")
        role_hard = _positive_int(raw.get("hard", fallback["hard"]), f"{role} 最大上下文容量")
        if role_soft > role_hard:
            raise ConfigurationError(f"{role} 的常用上下文不能大于最大上下文。")
        result[role] = {"soft": role_soft, "hard": role_hard}
    return result


def _agent_generation(value: Any) -> dict[str, dict[str, float | int | None]]:
    if not isinstance(value, dict):
        raise ConfigurationError("Agent 高级生成参数必须是对象。")
    result = {name: dict(defaults) for name, defaults in DEFAULT_ROLE_GENERATION.items()}
    for role, raw in value.items():
        if role not in result:
            raise ConfigurationError(f"不支持的 Agent 角色：{role}")
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{role} 的高级生成参数必须是对象。")
        extra = set(raw) - {"temperature", "top_p", "top_k"}
        if extra:
            raise ConfigurationError(f"{role} 包含不支持的高级参数：{', '.join(sorted(extra))}")
        try:
            temperature = float(raw.get("temperature", result[role]["temperature"]))
            top_p = float(raw.get("top_p", result[role]["top_p"]))
            top_k_value = raw.get("top_k", result[role]["top_k"])
            top_k = None if top_k_value in (None, "", 0, "0") else int(top_k_value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{role} 的高级生成参数必须是数字。") from exc
        if not 0 <= temperature <= 2:
            raise ConfigurationError(f"{role} 的 temperature 必须在 0～2 之间。")
        if not 0 < top_p <= 1:
            raise ConfigurationError(f"{role} 的 top_p 必须大于 0 且不超过 1。")
        if top_k is not None and not 1 <= top_k <= 200:
            raise ConfigurationError(f"{role} 的 top_k 必须留空，或设为 1～200。")
        result[role] = {"temperature": temperature, "top_p": top_p, "top_k": top_k}
    return result


def _role_models(value: Any, version: int | None) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigurationError("角色模型配置必须是对象。")
    if any(not isinstance(model, str) or len(model.strip()) > 200
           or any(ord(char) < 32 for char in model) for model in value.values()):
        raise ConfigurationError("角色模型 ID 必须是最多200字符的单行文本；留空继承默认模型。")
    migrated, _ = _migrate_settings_roles({role: {"model": model.strip()} for role, model in value.items()}, version)
    return {role: fields["model"] for role, fields in migrated.items()}
