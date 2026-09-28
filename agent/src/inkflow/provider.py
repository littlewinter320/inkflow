from __future__ import annotations

import asyncio
import json
import math
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .errors import ProviderError
from .utils import content_hash, strip_json_fence, estimate_tokens
from .runtime import active_runtime
from .model_usage import ModelAttempt, normalized_usage
from .role_protocol import roles_for_mode
from .task_settings import active_task_settings


T = TypeVar("T", bound=BaseModel)

_REQUEST_COUNTS_LOCK = threading.Lock()
_ACTIVE_MODEL_REQUESTS: dict[str, int] = {}


@asynccontextmanager
async def _model_request_slot(settings: Settings):
    """Limit actual HTTP generation requests across tasks and event loops."""

    endpoint = settings.base_url
    while True:
        with _REQUEST_COUNTS_LOCK:
            active = _ACTIVE_MODEL_REQUESTS.get(endpoint, 0)
            if active < settings.max_concurrent_model_requests:
                _ACTIVE_MODEL_REQUESTS[endpoint] = active + 1
                break
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        with _REQUEST_COUNTS_LOCK:
            remaining = _ACTIVE_MODEL_REQUESTS[endpoint] - 1
            if remaining:
                _ACTIVE_MODEL_REQUESTS[endpoint] = remaining
            else:
                del _ACTIVE_MODEL_REQUESTS[endpoint]


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    try:
        seconds = float(response.headers.get("retry-after", 2 ** attempt))
    except ValueError:
        seconds = float(2 ** attempt)
    return min(30.0, max(1.0, seconds if math.isfinite(seconds) else float(2 ** attempt)))


def _model_role(role: str | None) -> str | None:
    scope = active_task_settings.get()
    if scope is None or scope.role_protocol_version == 1:
        # Legacy reviewer is the combined Editor. Keep its stored role label.
        if role in {"reviewer_verifier", "reviewer_judge"}:
            return "reviewer"
        if role not in {None, "coordinator", "writer", "reviewer"}:
            raise ProviderError("旧版模型调用仅支持协调者、写作者和综合编辑者。")
        return role
    # The old verifier/judge helpers still belong to Editor, not specialist Reviewer.
    canonical = "editor" if role in {"reviewer_verifier", "reviewer_judge"} else role
    if canonical is not None and canonical not in roles_for_mode(scope.collaboration_mode):
        raise ProviderError("当前协作模式未启用该模型角色；引擎服务不能作为 Agent 调用。")
    return canonical


def _generation_settings(settings: Settings, role: str | None) -> dict[str, float | int | None]:
    if role is None:
        return {}
    scope = active_task_settings.get()
    version = scope.role_protocol_version if scope else 1
    return settings.generation_for(role, version)


@dataclass(slots=True)
class ProviderResult(Generic[T]):
    data: T
    model: str
    response_id: str | None
    reasoning_content: str | None
    usage: dict[str, Any]
    agent_role: str = ""


class JsonModelProvider(Protocol):
    async def get_balance(self) -> dict[str, Any]: ...

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: type[T],
        effort: str | None = None,
        max_tokens: int = 32_000,
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
        max_tokens: int = 32_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
        agent_role: str | None = None,
        model_override: str | None = None,
    ) -> ProviderResult[T]:
        agent_role = _model_role(agent_role)
        api_key = self.settings.require_api_key()
        schema = output_model.model_json_schema()
        # A canonical schema string keeps the long system-prefix byte-identical
        # across repeated calls of the same Agent/output contract.
        schema_prompt = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        system = (
            system_prompt.rstrip()
            + "\n\n你必须只输出一个合法 JSON 对象，不能使用 Markdown 代码围栏。"
            + "输出必须满足以下 JSON Schema：\n"
            + schema_prompt
        )
        # Hash only: diagnose exact-contract or early-prefix drift without
        # writing novel text or credentials to usage logs. A matching hash is
        # necessary, not sufficient, for a provider-side cache hit.
        prefix_diagnostic = {
            "prompt_family": f"{self.settings.provider_kind}:{model_override or self.settings.model}:"
                             f"{agent_role or 'unknown'}:{output_model.__name__}",
            "system_contract_hash": content_hash(system),
            "user_prefix_4096_hash": content_hash(user_prompt[:4096]),
        }
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
        if self.settings.provider_kind == "openai":
            payload["prompt_cache_key"] = (
                f"inkflow:{agent_role or 'unknown'}:{model_override or self.settings.model}:"
                f"{output_model.__name__}:v1"
            )
        generation = _generation_settings(self.settings, agent_role)
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
        recovery_events: list[dict[str, Any]] = []
        attempt_usage: list[dict[str, Any]] = []
        reported_usage: dict[str, int] = {}
        request_id = uuid4().hex
        for attempt in range(3):
            runtime = active_runtime.get()
            serialized_input = json.dumps(payload["messages"], ensure_ascii=False)
            reservation_output = requested_max_tokens
            reserved = runtime.reserve(
                estimate_tokens(serialized_input) + reservation_output
            ) if runtime else 0
            # One ledger entry per actual HTTP attempt, including format retries.
            # UTF-8 bytes plus framing allowance is deliberately conservative;
            # it is separate from the UI's approximate token estimate.
            accounting = ModelAttempt(self.settings, request_id=request_id,
                model=str(payload["model"]), role=agent_role or "unspecified",
                prompt_family=prefix_diagnostic["prompt_family"],
                input_estimate=estimate_tokens(serialized_input), max_output_tokens=requested_max_tokens,
                input_bound=len(serialized_input.encode("utf-8")) + 1024,
                task_id=runtime.task_id if runtime else "")
            retry_kind = "format"
            retry_delay = 0.0
            try:
                request_timeout = timeout_seconds or self.settings.request_timeout_seconds
                async with _model_request_slot(self.settings):
                    async with httpx.AsyncClient(timeout=request_timeout) as client:
                        response = await asyncio.wait_for(
                            client.post(url, headers=headers, json=payload),
                            timeout=request_timeout,
                        )
                if response.status_code >= 400:
                    accounting.failure = f"HTTP_{response.status_code}"
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
                    retry_kind = "transport" if response.status_code in {408, 429, 500, 502, 503, 504} else "permanent"
                    if retry_kind == "transport":
                        retry_delay = _retry_after_seconds(response, attempt)
                    provider_name = "DeepSeek" if self.settings.is_deepseek else "模型服务"
                    raise ProviderError(f"{provider_name} API 返回 HTTP {response.status_code}：{message}")
                body = response.json()
                usage = dict(body.get("usage") or {})
                accounting.settle(usage)
                attempt_usage.append({
                    "attempt": attempt + 1,
                    "thinking": payload.get("thinking"),
                    "usage": usage,
                })
                if runtime:
                    runtime.settle(reserved, usage)
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        reported_usage[key] = reported_usage.get(key, 0) + value
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
                accounting.outcome = "completed"
                return ProviderResult(
                    data=result,
                    model=str(body.get("model") or model_override or self.settings.model),
                    response_id=body.get("id"),
                    reasoning_content=message.get("reasoning_content"),
                    usage={**usage, **reported_usage, "attempt_usage": attempt_usage,
                           "cache_prefix_diagnostic": prefix_diagnostic,
                           **({"recovery_events": recovery_events} if recovery_events else {})},
                    agent_role=agent_role,
                )
            except asyncio.CancelledError:
                accounting.outcome = "cancelled"
                accounting.failure = "CancelledError"
                raise
            except (
                httpx.HTTPError,
                asyncio.TimeoutError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                ValidationError,
                ProviderError,
            ) as exc:
                accounting.failure = accounting.failure or type(exc).__name__
                last_error = exc
                if retry_kind == "permanent":
                    raise
                if isinstance(exc, (httpx.HTTPError, asyncio.TimeoutError)):
                    retry_kind = "transport"
                    retry_delay = float(2 ** attempt)
                if attempt < 2:
                    recovery_events.append({
                        "attempt": attempt + 1,
                        "kind": retry_kind,
                        "error_type": type(exc).__name__,
                        "strategy": "等待后重连，保留原任务" if retry_kind == "transport" else "修复 JSON 结构，保留完整内容要求",
                        "usage_unknown": isinstance(exc, (httpx.HTTPError, asyncio.TimeoutError)),
                    })
                    if runtime:
                        problem = "网络或限流" if retry_kind == "transport" else "输出格式"
                        runtime.publish({
                            "type": "model.recovering",
                            "summary": f"模型请求遇到{problem}问题，正在第 {attempt + 2}/3 次尝试；不重做已保存的正文或提交。",
                            "metadata": {"kind": retry_kind, "attempt": attempt + 2, "error_type": type(exc).__name__},
                        })
                    if retry_kind == "transport":
                        # Only model generation is retried, never a file/canon commit.
                        await asyncio.sleep(retry_delay)
                        continue
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
                        + "保持原任务的字数、内容和证据要求，只修复输出结构；所有必填字段都必须存在，只返回一个 JSON 对象。"
                        + ("校验缺口：" + "; ".join(
                            f"{'.'.join(map(str, item['loc']))}: {item['type']} - {item['msg']}"
                            for item in exc.errors(include_input=False)[:8]
                        ) if isinstance(exc, ValidationError) else "")
                    )
                    continue
                break
            finally:
                accounting.finish()
        if isinstance(last_error, (httpx.TimeoutException, asyncio.TimeoutError)):
            reason = f"连接或等待模型响应超时（{type(last_error).__name__}）；请检查网络和模型服务后继续，已有内容已保留。"
        else:
            reason = str(last_error).strip() or type(last_error).__name__
        raise ProviderError(f"模型请求经过 3 次限次自恢复仍未完成：{reason}") from last_error


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
        max_tokens: int = 32_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
        agent_role: str | None = None,
        model_override: str | None = None,
    ) -> ProviderResult[T]:
        agent_role = _model_role(agent_role)
        del effort, thinking
        schema_prompt = json.dumps(
            output_model.model_json_schema(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        generation = _generation_settings(self.settings, agent_role)
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
        runtime = active_runtime.get()
        serialized_input = payload["system"] + user_prompt
        request_id = uuid4().hex
        recovery_events: list[dict[str, Any]] = []
        for attempt in range(3):
            reserved = runtime.reserve(
                estimate_tokens(serialized_input) + int(payload["max_tokens"])
            ) if runtime else 0
            accounting = ModelAttempt(self.settings, request_id=request_id,
                model=str(payload["model"]), role=agent_role or "unspecified",
                input_estimate=estimate_tokens(serialized_input), max_output_tokens=int(payload["max_tokens"]),
                input_bound=len(serialized_input.encode("utf-8")) + 1024,
                task_id=runtime.task_id if runtime else "")
            try:
                async with _model_request_slot(self.settings):
                    async with httpx.AsyncClient(timeout=request_timeout) as client:
                        response = await asyncio.wait_for(
                            client.post(f"{self.settings.base_url}/v1/messages", headers=self._headers(), json=payload),
                            timeout=request_timeout,
                        )
                if response.status_code == 429 and attempt < 2:
                    accounting.failure = "HTTP_429"
                    recovery_events.append({
                        "attempt": attempt + 1,
                        "kind": "transport",
                        "error_type": "HTTP_429",
                        "strategy": "等待限流窗口后重试当前请求",
                        "usage_unknown": True,
                    })
                    if runtime:
                        runtime.publish({
                            "type": "model.recovering",
                            "summary": f"模型请求遇到供应商限流，正在第 {attempt + 2}/3 次尝试。",
                            "metadata": {"kind": "transport", "attempt": attempt + 2, "error_type": "HTTP_429"},
                        })
                    await asyncio.sleep(_retry_after_seconds(response, attempt))
                    continue
                if response.status_code >= 400:
                    accounting.failure = f"HTTP_{response.status_code}"
                    raise ProviderError(f"Anthropic API 返回 HTTP {response.status_code}：{_safe_error_message(response)}")
                body = response.json()
                blocks = body.get("content") or []
                raw_usage = dict(body.get("usage") or {})
                accounting.settle(raw_usage, anthropic=True)
                if runtime:
                    runtime.settle(reserved, raw_usage, anthropic=True)
                content = "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
                result = _model_from_json_content(content, output_model)
                usage = dict(raw_usage)
                counts = normalized_usage(raw_usage, anthropic=True)
                if counts["input_tokens"] is not None and counts["output_tokens"] is not None:
                    usage["prompt_tokens"] = counts["input_tokens"]
                    usage["completion_tokens"] = counts["output_tokens"]
                    usage["total_tokens"] = counts["input_tokens"] + counts["output_tokens"]
                    usage["prompt_cache_hit_tokens"] = counts["cache_hit_tokens"]
                    usage["prompt_cache_miss_tokens"] = counts["input_tokens"] - (counts["cache_hit_tokens"] or 0)
                accounting.outcome = "completed"
                return ProviderResult(
                    data=result,
                    model=str(body.get("model") or model_override or self.settings.model),
                    response_id=body.get("id"),
                    reasoning_content=None,
                    usage={**usage, **({"recovery_events": recovery_events} if recovery_events else {})},
                    agent_role=agent_role,
                )
            except asyncio.CancelledError:
                accounting.outcome = "cancelled"
                accounting.failure = "CancelledError"
                raise
            except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ProviderError) as exc:
                accounting.failure = accounting.failure or type(exc).__name__
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(f"Anthropic JSON 调用失败：{exc}") from exc
            finally:
                accounting.finish()
        raise ProviderError("Anthropic 模型请求经过 3 次限流重试仍未完成。")


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


def _prune_extra_json_fields(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    """Drop only fields forbidden by the requested JSON Schema.

    DeepSeek occasionally returns a useful nested object plus one explanatory
    key not present in the schema. Removing that key locally is lossless and
    avoids paying for three identical format retries; missing or mistyped
    required data still fails normal Pydantic validation.
    """

    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        if isinstance(target, dict):
            return _prune_extra_json_fields(value, target, root)
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(alternatives, list):
        for option in alternatives:
            if not isinstance(option, dict):
                continue
            option_type = option.get("type")
            if (option_type == "object" and isinstance(value, dict)) or (option_type == "array" and isinstance(value, list)):
                return _prune_extra_json_fields(value, option, root)
            if "$ref" in option and isinstance(value, dict):
                return _prune_extra_json_fields(value, option, root)
        return value
    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        return {
            key: _prune_extra_json_fields(item, properties[key], root)
            for key, item in value.items()
            if key in properties
        }
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        return [_prune_extra_json_fields(item, items, root) for item in value]
    return value


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
        # The ban on the Chinese enumeration comma is a deterministic output
        # format rule, not a creative decision. DeepSeek occasionally keeps a
        # few after otherwise valid retries. Normalize those marks before
        # schema validation so a punctuation slip cannot stop a whole batch.
        if (
            getattr(output_model, "__name__", "") == "DraftOutput"
            and isinstance(candidate, dict)
            and isinstance(candidate.get("content"), str)
            and "、" in candidate["content"]
        ):
            candidate = dict(candidate)
            candidate["content"] = candidate["content"].replace("、", "，")
        # 兼容 Reviewer 偶尔把一条 finding 直接放在顶层的情况。它仍然
        # 必须具备 finding 的完整字段；包装后继续走 ReviewReport 的严格
        # 校验与后续证据门禁，不能因此自动放行。
        if getattr(output_model, "__name__", "") == "ReviewReport" and isinstance(candidate, dict):
            finding_fields = {"category", "severity", "evidence", "explanation", "repair_instruction"}
            if finding_fields.issubset(candidate):
                allowed_finding_fields = {
                    "category", "severity", "evidence", "canon_refs", "explanation",
                    "repair_instruction", "rule_id", "reference_evidence", "verification_note",
                    "claim", "verification_status", "semantic_status", "verification_confidence",
                    "proposed_severity",
                }
                finding = {key: value for key, value in candidate.items() if key in allowed_finding_fields}
                wrapped = {
                    "verdict": "patch" if str(finding.get("severity")) in {"major", "blocking"} else "unknown",
                    "confidence": float(candidate.get("verification_confidence") or 0.5),
                    "summary": "模型返回单条审查意见，已按兼容格式归档并继续执行证据核验。",
                    "strengths": [],
                    "findings": [finding],
                    "scorecard": [],
                    "source_hash": "",
                    "hook_assessment": None,
                    "context_use_audit": {},
                }
                try:
                    return output_model.model_validate(wrapped)
                except ValidationError as exc:
                    last_validation = exc
        try:
            return output_model.model_validate(candidate)
        except ValidationError as exc:
            last_validation = exc
            errors = exc.errors(include_input=False)
            if errors and all(item.get("type") == "extra_forbidden" for item in errors):
                root_schema = output_model.model_json_schema()
                pruned = _prune_extra_json_fields(candidate, root_schema, root_schema)
                if pruned != candidate:
                    try:
                        return output_model.model_validate(pruned)
                    except ValidationError as pruned_error:
                        last_validation = pruned_error
            # 模型有时会在合法结构外附带一两个顶层说明字段（例如
            # verification_note）。这些字段不属于目标 Schema，也不会改变
            # 任何业务判断；先移除未知顶层键，再重新执行完整的必填/类型校验。
            # 缺少必填字段或嵌套结构错误仍会继续抛出 ValidationError。
            if isinstance(candidate, dict):
                known_fields = set(getattr(output_model, "model_fields", {}) or {})
                if known_fields and set(candidate) - known_fields:
                    trimmed = {key: value for key, value in candidate.items() if key in known_fields}
                    try:
                        return output_model.model_validate(trimmed)
                    except ValidationError as trimmed_error:
                        last_validation = trimmed_error
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
        max_tokens: int = 32_000,
        thinking: bool = True,
        timeout_seconds: float | None = None,
        agent_role: str | None = None,
        model_override: str | None = None,
    ) -> ProviderResult[T]:
        agent_role = _model_role(agent_role)
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
            agent_role=agent_role,
        )
