from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


KEYRING_SERVICE = "InkFlow"
KEYRING_ACCOUNT = "deepseek_api_key"


@dataclass(slots=True)
class Settings:
    """运行时配置。敏感字段不序列化到项目文件。"""

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    reasoning_effort: str = "high"
    context_soft_tokens: int = 256_000
    context_hard_tokens: int = 512_000
    request_timeout_seconds: float = 180.0
    planning_timeout_seconds: float = 600.0
    trace_level: str = "full"
    show_provider_reasoning: bool = True
    workspace_root: Path | None = None

    @classmethod
    def from_env(cls, workspace_root: str | Path | None = None) -> "Settings":
        effort = os.getenv("INKFLOW_REASONING_EFFORT", "high").lower()
        if effort not in {"low", "high", "max"}:
            effort = "high"
        trace_level = os.getenv("INKFLOW_TRACE_LEVEL", "full").lower()
        if trace_level not in {"compact", "standard", "full"}:
            trace_level = "full"
        return cls(
            base_url=os.getenv("INKFLOW_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("INKFLOW_MODEL", "deepseek-v4-flash"),
            reasoning_effort=effort,
            context_soft_tokens=int(os.getenv("INKFLOW_CONTEXT_SOFT_TOKENS", "256000")),
            context_hard_tokens=int(os.getenv("INKFLOW_CONTEXT_HARD_TOKENS", "512000")),
            request_timeout_seconds=float(os.getenv("INKFLOW_TIMEOUT_SECONDS", "180")),
            planning_timeout_seconds=float(os.getenv("INKFLOW_PLANNING_TIMEOUT_SECONDS", "600")),
            trace_level=trace_level,
            show_provider_reasoning=os.getenv("INKFLOW_SHOW_REASONING", "1") not in {"0", "false", "False"},
            workspace_root=Path(workspace_root).resolve() if workspace_root else None,
        )

    def get_api_key(self) -> str | None:
        value = os.getenv("DEEPSEEK_API_KEY")
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
                "未找到 DeepSeek API Key。请设置 DEEPSEEK_API_KEY，"
                "或让宿主 Agent 运行 `inkflow configure-key` 后由用户在安全提示中输入。"
            )
        return key


def save_api_key_to_keyring(api_key: str) -> None:
    if not api_key.strip():
        raise ConfigurationError("API Key 不能为空。")
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, api_key.strip())
    except Exception as exc:
        raise ConfigurationError(f"无法写入系统凭据库：{exc}") from exc
