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
        agent_role: str | None = None,
        model_override: str | None = None,
    ) -> ProviderResult[T]: ...

    def capabilities(self) -> dict[str, Any]: ...

    async def list_models(self) -> list[str]: ...


class DeepSeekProvider:
    """OpenAI 兼容 Chat Completions 适配器，并为 DeepSeek 启用官方扩展字段。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, Any]:
        kind = self.settings.provider_kind
        return {
            "provider": kind,
            "protocol": "openai_compatible",
            "json_mode": kind != "ollama",
            "reasoning_effort": kind in {"deepseek", "openai"},
            "top_k": False,
            "balance": self.settings.is_deepseek,
            "api_key_required": kind != "ollama",
            "context_hard_tokens": self.settings.context_hard_tokens,
            "max_output_tokens": self.settings.max_output_tokens,
        }

    async def list_models(self) -> list[str]:
        headers = {"Content-Type": "application/json"}
        api_key = self.settings.require_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=min(self.settings.request_timeout_seconds, 30.0)) as client:
                response = await client.get(f"{self.settings.base_url}/models", headers=headers)
            if response.status_code >= 400:
                raise ProviderError(f"模型列表接口返回 HTTP {response.status_code}：{_safe_error_message(response)}")
            body = response.json()
            values = body.get("data", body.get("models", [])) if isinstance(body, dict) else []
            models = [str(item.get("id") or item.get("name")) for item in values if isinstance(item, dict)]
            return sorted({item for item in models if item})
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"无法读取模型列表：{exc}") from exc

    async def get_balance(self) -> dict[str, Any]:
        if not self.settings.is_deepseek:
            raise ProviderError("当前是自定义 OpenAI 兼容接口，未声明 DeepSeek 余额能力。")
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
        agent_role: str | None = None,
        model_override: str | None = None,
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
        requested_max_tokens = min(max(1, int(max_tokens)), self.settings.max_output_tokens)
        payload: dict[str, Any] = {
            "model": model_override or self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": requested_max_tokens,
            "stream": False,
        }
        generation = self.settings.agent_generation.get(agent_role or "")
        if generation:
            payload["temperature"] = generation["temperature"]
            payload["top_p"] = generation["top_p"]
            if generation["top_k"] is not None:
                payload["top_k"] = generation["top_k"]
        if self.settings.is_deepseek:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if thinking and self.settings.is_deepseek:
            payload["reasoning_effort"] = effort or self.settings.reasoning_effort
        url = f"{self.settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if not api_key:
            headers.pop("Authorization", None)

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
                    if attempt == 0 and "top_k" in payload and "top_k" in message.casefold():
                        # top_k 不是 OpenAI Chat Completions 的通用字段。兼容接口明确拒绝时，
                        # 只撤掉这个可选参数并重试；temperature/top_p 仍保持用户设置。
                        payload.pop("top_k", None)
                        continue
                    if (
                        attempt == 0
                        and not self.settings.is_deepseek
                        and response.status_code in {400, 404, 422}
                        and "response_format" in payload
                    ):
                        payload.pop("response_format", None)
                        continue
                    provider_name = "DeepSeek" if self.settings.is_deepseek else "模型服务"
                    raise ProviderError(f"{provider_name} API 返回 HTTP {response.status_code}：{message}")
                body = response.json()
                choice = body["choices"][0]
                message = choice["message"]
                content = message.get("content") or ""
                if not content:
                    usage = dict(body.get("usage") or {})
                    detail = usage.get("completion_tokens_details") or {}
                    raise ProviderError(
                        "模型服务返回了空 JSON 内容"
                        f"（finish_reason={choice.get('finish_reason') or 'unknown'}, "
                        f"completion_tokens={usage.get('completion_tokens', 'unknown')}, "
                        f"reasoning_tokens={detail.get('reasoning_tokens', 'unknown')}）。"
                    )
                result = _model_from_json_content(content, output_model)
                return ProviderResult(
                    data=result,
                    model=str(body.get("model") or model_override or self.settings.model),
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
                        f"模型服务在 {request_timeout:.0f} 秒内未返回有效 JSON；为避免不确定的重复计费，"
                        "本次请求不会自动重试。保留草稿与 Trace 后可由用户或长跑协调器恢复。"
                    ) from exc
                if attempt == 0:
                    # 对 DeepSeek 而言，空内容且 finish_reason=length 通常表示推理
                    # 已经耗尽预算，却没有留下最终 JSON。第二次机会应优先交付
                    # 可解析的答案：关闭推理，且绝不因为重试而突破本次任务预算。
                    if self.settings.is_deepseek:
                        payload["thinking"] = {"type": "disabled"}
                        payload.pop("reasoning_effort", None)
                    payload["max_tokens"] = requested_max_tokens
                    payload["messages"][1]["content"] = (
                        user_prompt
                        + "\n\n上一次输出为空、截断或不符合 Schema。此轮不展开推理，"
                        + "优先交付最短的完整 JSON；所有必填字段都必须存在，只返回一个 JSON 对象。"
                    )
                    continue
                break
        raise ProviderError(f"模型 JSON 调用两次均未通过验证：{last_error}") from last_error


class AnthropicProvider:
    """Anthropic Messages API 适配器。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "anthropic",
            "protocol": "anthropic_messages",
            "json_mode": False,
            "reasoning_effort": False,
            "top_k": True,
            "balance": False,
            "api_key_required": True,
            "context_hard_tokens": self.settings.context_hard_tokens,
            "max_output_tokens": self.settings.max_output_tokens,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.settings.require_api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=min(self.settings.request_timeout_seconds, 30.0)) as client:
                response = await client.get(f"{self.settings.base_url}/v1/models", headers=self._headers())
            if response.status_code >= 400:
                raise ProviderError(f"Anthropic 模型列表返回 HTTP {response.status_code}：{_safe_error_message(response)}")
            body = response.json()
            return sorted(
                {str(item.get("id")) for item in body.get("data", []) if isinstance(item, dict) and item.get("id")}
            )
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"无法读取 Anthropic 模型列表：{exc}") from exc

    async def get_balance(self) -> dict[str, Any]:
        raise ProviderError("Anthropic Messages API 不提供余额查询；请在服务商控制台查看。")

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
        agent_role: str | None = None,
        model_override: str | None = None,
    ) -> ProviderResult[T]:
        del effort, thinking
        schema_prompt = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
        generation = self.settings.agent_generation.get(agent_role or "", {})
        payload: dict[str, Any] = {
            "model": model_override or self.settings.model,
            "system": system_prompt.rstrip() + "\n\n只输出满足下列 JSON Schema 的 JSON 对象，不要使用 Markdown：\n" + schema_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": min(max(1, int(max_tokens)), self.settings.max_output_tokens),
        }
        if generation:
            payload["temperature"] = generation.get("temperature")
            payload["top_p"] = generation.get("top_p")
            if generation.get("top_k") is not None:
                payload["top_k"] = generation["top_k"]
        request_timeout = timeout_seconds or self.settings.request_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await asyncio.wait_for(
                    client.post(f"{self.settings.base_url}/v1/messages", headers=self._headers(), json=payload),
                    timeout=request_timeout,
                )
            if response.status_code >= 400:
                raise ProviderError(f"Anthropic API 返回 HTTP {response.status_code}：{_safe_error_message(response)}")
            body = response.json()
            blocks = body.get("content") or []
            content = "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
            result = _model_from_json_content(content, output_model)
            usage = dict(body.get("usage") or {})
            usage.setdefault("prompt_tokens", usage.get("input_tokens", 0))
            usage.setdefault("completion_tokens", usage.get("output_tokens", 0))
            usage.setdefault("total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
            return ProviderResult(
                data=result,
                model=str(body.get("model") or model_override or self.settings.model),
                response_id=body.get("id"),
                reasoning_content=None,
                usage=usage,
            )
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ProviderError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"Anthropic JSON 调用失败：{exc}") from exc


def create_provider(settings: Settings) -> JsonModelProvider:
    if settings.provider_kind == "anthropic":
        return AnthropicProvider(settings)
    return DeepSeekProvider(settings)


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
        agent_role: str | None = None,
        model_override: str | None = None,
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
                "agent_role": agent_role,
                "model_override": model_override,
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
