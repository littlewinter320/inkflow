from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigurationError


KEYRING_SERVICE = "InkFlow"
KEYRING_ACCOUNT = "deepseek_api_key"
SETTINGS_ENV = "INKFLOW_SETTINGS_PATH"
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "max"}
PERSISTED_SETTING_NAMES = {
    "base_url",
    "model",
    "reasoning_effort",
    "context_soft_tokens",
    "context_hard_tokens",
    "max_output_tokens",
    "request_timeout_seconds",
    "planning_timeout_seconds",
    "trace_level",
    "show_provider_reasoning",
}


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
    return {key: value[key] for key in PERSISTED_SETTING_NAMES if key in value}


def save_user_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """只保存白名单内的非敏感设置，并以替换文件的方式原子写入。"""

    forbidden = {key for key in updates if "key" in key.lower() or "secret" in key.lower()}
    if forbidden:
        raise ConfigurationError("API Key 或其他密钥不能写入设置文件。")
    unknown = set(updates) - PERSISTED_SETTING_NAMES
    if unknown:
        raise ConfigurationError(f"不支持的设置项：{', '.join(sorted(unknown))}")
    current = load_user_settings()
    current.update(updates)
    validated = Settings.from_mapping(current)
    clean = {
        key: value
        for key, value in asdict(validated).items()
        if key in PERSISTED_SETTING_NAMES
    }
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
    return clean


@dataclass(slots=True)
class Settings:
    """运行时配置；敏感字段永远不序列化到项目或用户设置文件。"""

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    reasoning_effort: str = "high"
    context_soft_tokens: int = 256_000
    context_hard_tokens: int = 512_000
    max_output_tokens: int = 16_000
    request_timeout_seconds: float = 180.0
    planning_timeout_seconds: float = 600.0
    trace_level: str = "full"
    show_provider_reasoning: bool = True
    workspace_root: Path | None = None

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        workspace_root: str | Path | None = None,
    ) -> "Settings":
        defaults = cls()
        effort = str(value.get("reasoning_effort", defaults.reasoning_effort)).lower()
        if effort not in ALLOWED_REASONING_EFFORTS:
            raise ConfigurationError("思考强度只能是 low、medium、high 或 max。")
        trace_level = str(value.get("trace_level", defaults.trace_level)).lower()
        if trace_level not in {"compact", "standard", "full"}:
            raise ConfigurationError("过程记录级别只能是 compact、standard 或 full。")
        base_url = str(value.get("base_url", defaults.base_url)).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("模型接口地址必须是完整的 http 或 https 地址。")
        model = str(value.get("model", defaults.model)).strip()
        if not model:
            raise ConfigurationError("模型名称不能为空。")
        soft = _positive_int(value.get("context_soft_tokens", defaults.context_soft_tokens), "软上下文预算")
        hard = _positive_int(value.get("context_hard_tokens", defaults.context_hard_tokens), "硬上下文上限")
        output = _positive_int(value.get("max_output_tokens", defaults.max_output_tokens), "单次输出上限")
        if soft > hard:
            raise ConfigurationError("软上下文预算不能大于硬上下文上限。")
        if output > 16_000:
            raise ConfigurationError("墨流 0.2 的单次模型输出上限固定为 16K tokens。")
        timeout = _positive_float(
            value.get("request_timeout_seconds", defaults.request_timeout_seconds), "普通请求超时"
        )
        planning_timeout = _positive_float(
            value.get("planning_timeout_seconds", defaults.planning_timeout_seconds), "规划请求超时"
        )
        return cls(
            base_url=base_url,
            model=model,
            reasoning_effort=effort,
            context_soft_tokens=soft,
            context_hard_tokens=hard,
            max_output_tokens=output,
            request_timeout_seconds=timeout,
            planning_timeout_seconds=planning_timeout,
            trace_level=trace_level,
            show_provider_reasoning=_as_bool(
                value.get("show_provider_reasoning", defaults.show_provider_reasoning)
            ),
            workspace_root=Path(workspace_root).resolve() if workspace_root else None,
        )

    @classmethod
    def from_env(cls, workspace_root: str | Path | None = None) -> "Settings":
        value = load_user_settings()
        environment_mapping = {
            "base_url": "INKFLOW_BASE_URL",
            "model": "INKFLOW_MODEL",
            "reasoning_effort": "INKFLOW_REASONING_EFFORT",
            "context_soft_tokens": "INKFLOW_CONTEXT_SOFT_TOKENS",
            "context_hard_tokens": "INKFLOW_CONTEXT_HARD_TOKENS",
            "max_output_tokens": "INKFLOW_MAX_OUTPUT_TOKENS",
            "request_timeout_seconds": "INKFLOW_TIMEOUT_SECONDS",
            "planning_timeout_seconds": "INKFLOW_PLANNING_TIMEOUT_SECONDS",
            "trace_level": "INKFLOW_TRACE_LEVEL",
            "show_provider_reasoning": "INKFLOW_SHOW_REASONING",
        }
        for field, environment_name in environment_mapping.items():
            if environment_name in os.environ:
                value[field] = os.environ[environment_name]
        return cls.from_mapping(value, workspace_root)

    @property
    def is_deepseek(self) -> bool:
        return "deepseek" in urlparse(self.base_url).netloc.casefold()

    def get_api_key(self) -> str | None:
        for name in ("INKFLOW_API_KEY", "DEEPSEEK_API_KEY"):
            value = os.getenv(name)
            if value:
                return value.strip()
        try:
            import keyring

            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            return stored.strip() if stored else None
        except Exception:
            return None

    def require_api_key(self) -> str:
        key = self.get_api_key()
        if not key:
            raise ConfigurationError(
                "未找到模型 API Key。请在墨流设置中保存到 Windows 凭据库，"
                "或设置 INKFLOW_API_KEY / DEEPSEEK_API_KEY 环境变量。"
            )
        return key


def api_key_status() -> dict[str, Any]:
    """只返回来源和是否存在，不读取到调用者，更不返回密钥本身。"""

    if os.getenv("INKFLOW_API_KEY"):
        return {"api_key_configured": True, "api_key_storage": "environment:INKFLOW_API_KEY"}
    if os.getenv("DEEPSEEK_API_KEY"):
        return {"api_key_configured": True, "api_key_storage": "environment:DEEPSEEK_API_KEY"}
    try:
        import keyring

        configured = bool(keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT))
    except Exception:
        configured = False
    return {
        "api_key_configured": configured,
        "api_key_storage": "windows_credential_manager" if configured else "not_configured",
    }


def save_api_key_to_keyring(api_key: str) -> None:
    if not api_key.strip():
        raise ConfigurationError("API Key 不能为空。")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, api_key.strip())
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


def _positive_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label}必须是数字。") from exc
    if result <= 0:
        raise ConfigurationError(f"{label}必须大于零。")
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off"}
