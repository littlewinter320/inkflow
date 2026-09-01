from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .errors import ProviderError
from .utils import strip_json_fence


T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class ProviderResult(Generic[T]):
    data: T
    model: str
    response_id: str | None
    reasoning_content: str | None
    usage: dict[str, Any]


class JsonModelProvider(Protocol):
    async def get_balance(self) -> dict[str, Any]: ...

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        effort: str | None = None,
        max_tokens: int = 16_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]: ...


class DeepSeekProvider:
    """DeepSeek 官方 Chat Completions JSON 输出适配器。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def get_balance(self) -> dict[str, Any]:
        api_key = self.settings.require_api_key()
        headers = {"Authorization": f"Bearer {api_key}"}
        url = f"{self.settings.base_url}/user/balance"
        try:
            async with httpx.AsyncClient(timeout=min(self.settings.request_timeout_seconds, 30.0)) as client:
                response = await client.get(url, headers=headers)
            if response.status_code >= 400:
                raise ProviderError(
                    f"DeepSeek 余额接口返回 HTTP {response.status_code}：{_safe_error_message(response)}"
                )
            body = response.json()
            infos = list(body.get("balance_infos") or [])
            cny = next((item for item in infos if item.get("currency") == "CNY"), None)
            if cny is None:
                raise ProviderError("DeepSeek 余额接口未返回 CNY 余额，不能安全执行长跑。")
            return {
                "is_available": bool(body.get("is_available")),
                "currency": "CNY",
                "total_balance": float(cny["total_balance"]),
                "granted_balance": float(cny.get("granted_balance") or 0),
                "topped_up_balance": float(cny.get("topped_up_balance") or 0),
            }
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"无法可靠读取 DeepSeek CNY 余额：{exc}") from exc

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        effort: str | None = None,
        max_tokens: int = 16_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]:
        api_key = self.settings.require_api_key()
        schema = output_model.model_json_schema()
        schema_prompt = json.dumps(schema, ensure_ascii=False)
        system = (
            system_prompt.rstrip()
            + "\n\n你必须只输出一个合法 JSON 对象，不能使用 Markdown 代码围栏。"
            + "输出必须满足以下 JSON Schema：\n"
            + schema_prompt
        )
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": False,
        }
        if thinking:
            payload["reasoning_effort"] = effort or self.settings.reasoning_effort
        url = f"{self.settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                request_timeout = timeout_seconds or self.settings.request_timeout_seconds
                async with httpx.AsyncClient(timeout=request_timeout) as client:
                    response = await asyncio.wait_for(
                        client.post(url, headers=headers, json=payload),
                        timeout=request_timeout,
                    )
                if response.status_code >= 400:
                    message = _safe_error_message(response)
                    raise ProviderError(f"DeepSeek API 返回 HTTP {response.status_code}：{message}")
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                content = message.get("content") or ""
                if not content:
                    usage = dict(body.get("usage") or {})
                    detail = usage.get("completion_tokens_details") or {}
                    raise ProviderError(
                        "DeepSeek 返回了空 JSON 内容"
                        f"（finish_reason={choice.get('finish_reason') or 'unknown'}, "
                        f"completion_tokens={usage.get('completion_tokens', 'unknown')}, "
                        f"reasoning_tokens={detail.get('reasoning_tokens', 'unknown')}）。"
                    )
                result = _model_from_json_content(content, output_model)
                return ProviderResult(
                    data=result,
                    model=str(body.get("model") or self.settings.model),
                    response_id=body.get("id"),
                    reasoning_content=message.get("reasoning_content"),
                    usage=dict(body.get("usage") or {}),
                )
            except (
                httpx.HTTPError,
                asyncio.TimeoutError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                ValidationError,
                ProviderError,
            ) as exc:
                last_error = exc
                if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
                    raise ProviderError(
                        f"DeepSeek 在 {request_timeout:.0f} 秒内未返回有效 JSON；为避免不确定的重复计费，"
                        "本次请求不会自动重试。保留草稿与 Trace 后可由用户或长跑协调器恢复。"
                    ) from exc
                if attempt == 0:
                    # 第二次重试仍受任务原始预算量级约束，不能让小型分类/校对任务
                    # 因首次输出为空而突然膨胀到 24K。
                    if thinking:
                        payload["reasoning_effort"] = "low"
                    retry_floor = 6_000 if thinking else 2_000
                    payload["max_tokens"] = min(max(max_tokens * 2, retry_floor), 64_000)
                    payload["messages"][1]["content"] = (
                        user_prompt
                        + "\n\n上一次输出为空、截断或不符合 Schema。请压缩内部推理，"
                        + "优先保留最终答案；重新检查所有必填字段，只返回一个完整 JSON 对象。"
                    )
                    continue
                break
        raise ProviderError(f"DeepSeek JSON 调用两次均未通过验证：{last_error}") from last_error


def _safe_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or "未知错误")[:500]
    except Exception:
        pass
    return response.text[:500] or "未知错误"


def _model_from_json_content(content: str, output_model: type[T]) -> T:
    """容忍代码围栏或 JSON 前后的少量说明，但最终仍执行严格 Schema 校验。"""

    cleaned = strip_json_fence(content).strip()
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(cleaned, strict=False))
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder(strict=False)
        for index, char in enumerate(cleaned):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                candidates.append(value)
        if not candidates:
            raise direct_error

    last_validation: ValidationError | None = None
    for candidate in candidates:
        try:
            return output_model.model_validate(candidate)
        except ValidationError as exc:
            last_validation = exc
    assert last_validation is not None
    raise last_validation


class ScriptedProvider:
    """用于离线测试与演示的确定性 Provider。"""

    def __init__(self, responses: list[BaseModel | dict[str, Any]], balance_cny: float = 999.0):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.balance_cny = balance_cny
        self.balance_checks = 0

    async def get_balance(self) -> dict[str, Any]:
        self.balance_checks += 1
        return {
            "is_available": self.balance_cny > 0,
            "currency": "CNY",
            "total_balance": float(self.balance_cny),
            "granted_balance": 0.0,
            "topped_up_balance": float(self.balance_cny),
        }

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        effort: str | None = None,
        max_tokens: int = 16_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]:
        if not self.responses:
            raise ProviderError("ScriptedProvider 没有剩余响应。")
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "output_model": output_model.__name__,
                "effort": effort,
                "max_tokens": max_tokens,
                "thinking": thinking,
                "timeout_seconds": timeout_seconds,
            }
        )
        value = self.responses.pop(0)
        if isinstance(value, output_model):
            data = value
        elif isinstance(value, BaseModel):
            data = output_model.model_validate(value.model_dump())
        else:
            data = output_model.model_validate(value)
        return ProviderResult(
            data=data,
            model="scripted-mvp",
            response_id=f"scripted-{len(self.calls)}",
            reasoning_content="离线脚本响应：仅验证工作流，不代表真实模型推理。",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
