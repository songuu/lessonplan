"""An asynchronous OpenAI-compatible streaming boundary with auditable attempts."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .observability import step

UNCERTAIN_RESPONSE_CODES = frozenset({
    "provider_incomplete_stream", "provider_stream_error", "provider_invalid_response",
    "provider_response_limit", "provider_empty_response",
})


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, calls: list[dict] | None = None,
                 outcome_unknown: bool = False):
        super().__init__(message)
        self.code, self.message, self.calls = code, message, calls or []
        self.outcome_unknown = outcome_unknown


@dataclass
class Completion:
    content: str
    log: dict


async def invoke(callback: Callable | None, *args: Any) -> None:
    if callback is not None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result


def setting(settings: Any, name: str, default: Any = None) -> Any:
    value = settings.get(name, default) if isinstance(settings, dict) else getattr(settings, name, default)
    return default if value is None else value


class LLMGateway:
    def __init__(self, settings: Any, *, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    def _route(self, route: str, backup: bool) -> dict:
        prefix = "llm_fallback_" if backup else "llm_"
        model = setting(self.settings, f"{prefix}{route}_model") or setting(self.settings, f"{prefix}model", "")
        return {
            "model": model,
            "base_url": setting(self.settings, f"{prefix}base_url") or setting(self.settings, "llm_base_url", ""),
            "api_key": setting(self.settings, f"{prefix}api_key") or setting(self.settings, "llm_api_key", ""),
        }

    async def complete(self, messages: list[dict], *, route: str, prompt_version: str = "",
                       on_delta: Callable | None = None, on_reset: Callable | None = None,
                       mock_content: str | None = None, strict_outcome: bool = False) -> Completion:
        provider = setting(self.settings, "ai_provider", "mock")
        started = time.monotonic()
        if provider == "mock":
            if mock_content is None:
                raise GatewayError("mock_response_missing", "演示响应未显式提供。")
            for offset in range(0, len(mock_content), 48):
                await asyncio.sleep(0)
                await invoke(on_delta, mock_content[offset:offset + 48])
            return Completion(mock_content, {
                "component_name": route, "component_kind": "llm_agent", "llm_provider": "mock",
                "model_name": "deterministic-demo-v1", "prompt_version": prompt_version,
                "call_outcome": "success", "fallback_used": False, "primary_attempt_outcome": "success",
                "elapsed_ms": round((time.monotonic() - started) * 1000), "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0, "attempts": [], "is_demo": True,
            })
        if provider not in {"openai", "lesson_plan_ai", "openai_compatible"}:
            raise GatewayError("provider_not_supported", "AI Provider 配置不受支持。")
        attempts: list[dict] = []
        last_error: GatewayError | None = None
        for backup in (False, True):
            config = self._route(route, backup)
            if backup and not config["model"]:
                break
            parsed = urlsplit(config["base_url"])
            if not config["model"] or not config["api_key"] or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise GatewayError("provider_configuration", "AI Provider 的地址、模型和密钥配置不完整。", calls=attempts)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise GatewayError("provider_configuration", "AI Provider 地址不能包含身份凭证、查询参数或片段。")
            if backup:
                await invoke(on_reset)
            attempt_start = time.monotonic()
            try:
                timeout = float(setting(self.settings, f"llm_{route}_timeout_seconds",
                                        setting(self.settings, "llm_timeout_seconds", 120)))
                with step("llm.request", run_type="llm", route=route,
                          ls_provider="openai_compatible", ls_model_name=config["model"],
                          attempt_role="backup" if backup else "primary",
                          prompt_version=prompt_version) as request_span:
                    if request_span is not None:
                        request_span.set_input(messages)
                    first_delta = False

                    async def observed_delta(delta, *, observed_span=request_span, started_at=attempt_start):
                        nonlocal first_delta
                        if delta.strip() and not first_delta:
                            first_delta = True
                            if observed_span is not None:
                                observed_span.event("new_token")
                                observed_span.set(first_token_ms=round((time.monotonic() - started_at) * 1000))
                        await invoke(on_delta, delta)

                    try:
                        async with asyncio.timeout(timeout):
                            content, usage = await self._request(config, messages, route, timeout, observed_delta)
                    except (TimeoutError, httpx.TimeoutException):
                        if request_span is not None:
                            request_span.fail("provider_timeout")
                            request_span.set(transport_outcome="unknown", usage_known=False)
                        raise
                    except httpx.HTTPError:
                        if request_span is not None:
                            request_span.fail("provider_network_error")
                            request_span.set(transport_outcome="unknown", usage_known=False)
                        raise
                    except GatewayError as exc:
                        if request_span is not None:
                            request_span.fail(exc.code)
                            unknown = exc.outcome_unknown or (strict_outcome and exc.code in UNCERTAIN_RESPONSE_CODES)
                            request_span.set(transport_outcome="unknown" if unknown else "failed",
                                             usage_known=False)
                        raise
                    if request_span is not None:
                        request_span.set_output(content)
                        request_span.set(transport_outcome="success")
                        request_span.usage({target: usage[source] for source, target in (
                            ("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                            ("total_tokens", "total_tokens")) if source in usage})
                attempts.append({"provider": "backup" if backup else "primary", "model_name": config["model"],
                                 "outcome": "success", "elapsed_ms": round((time.monotonic() - attempt_start) * 1000)})
                log = {"component_name": route, "component_kind": "llm_agent", "llm_provider": "openai_compatible",
                       "model_name": config["model"], "prompt_version": prompt_version,
                       "call_outcome": "success", "fallback_used": backup,
                       "primary_attempt_outcome": "failed" if backup else "success",
                       "elapsed_ms": round((time.monotonic() - started) * 1000), "attempts": attempts,
                       **usage, "usage_reported": all(key in usage for key in ("prompt_tokens", "completion_tokens")),
                       "is_demo": False}
                return Completion(content, log)
            except (TimeoutError, httpx.TimeoutException):
                last_error = GatewayError("provider_timeout", "AI 服务响应超时，请稍后重试。", outcome_unknown=True)
            except httpx.HTTPError:
                last_error = GatewayError("provider_network_error", "AI 服务连接异常，请稍后重试。", outcome_unknown=True)
            except GatewayError as exc:
                last_error = exc
            attempts.append({"provider": "backup" if backup else "primary", "model_name": config["model"],
                             "outcome": "failed", "error_code": last_error.code,
                             "elapsed_ms": round((time.monotonic() - attempt_start) * 1000)})
            if strict_outcome and (last_error.outcome_unknown or last_error.code in UNCERTAIN_RESPONSE_CODES):
                # A second transport cannot safely replace a request that may already have been billed.
                last_error.outcome_unknown = True
                break
        assert last_error is not None
        failed_log = {"component_name": route, "component_kind": "llm_agent", "llm_provider": "openai_compatible",
                      "model_name": attempts[-1]["model_name"], "prompt_version": prompt_version,
                      "call_outcome": "unknown" if strict_outcome and last_error.outcome_unknown else "failed",
                      "fallback_used": len(attempts) > 1,
                      "primary_attempt_outcome": "failed", "error_code": last_error.code,
                      "elapsed_ms": round((time.monotonic() - started) * 1000), "attempts": attempts}
        raise GatewayError(last_error.code, last_error.message, calls=[failed_log],
                           outcome_unknown=last_error.outcome_unknown)

    async def _request(self, config: dict, messages: list[dict], route: str, timeout: float,
                       on_delta: Callable | None) -> tuple[str, dict]:
        payload = {"model": config["model"], "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True},
                   "max_tokens": int(setting(self.settings, f"llm_{route}_max_tokens",
                                              setting(self.settings, "llm_max_tokens", 8192)))}
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        chunks: list[str] = []
        char_count = 0
        finished = False
        usage = {}

        def metadata(data):
            if not isinstance(data, dict) or not isinstance(data.get("choices", []), list):
                raise GatewayError("provider_invalid_response", "AI 服务响应格式无效。", outcome_unknown=True)
            request_id = data.get("id")
            if (isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", request_id)
                and config["api_key"] not in request_id):
                usage.setdefault("provider_request_id", request_id)
            for choice in data.get("choices", []):
                reason = choice.get("finish_reason")
                if reason in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
                    usage["finish_reason"] = reason
        try:
            async with client.stream("POST", config["base_url"].rstrip("/") + "/chat/completions",
                                     headers={"Authorization": f"Bearer {config['api_key']}",
                                              "Accept": "text/event-stream"},
                                     json=payload, timeout=timeout) as response:
                if response.status_code >= 400:
                    code = "provider_rate_limited" if response.status_code == 429 else "provider_http_error"
                    raise GatewayError(code, f"AI 服务返回受控错误（HTTP {response.status_code}）。",
                                       outcome_unknown=response.status_code >= 500)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raw = await response.aread()
                    if len(raw) > 4 * 1024 * 1024:
                        raise GatewayError("provider_response_limit", "AI 响应超出安全大小。")
                    try:
                        data = json.loads(raw)
                        metadata(data)
                        reason = data["choices"][0].get("finish_reason")
                        if reason == "length":
                            raise GatewayError("provider_truncated", "AI 正文达到长度上限，请精简后重试。")
                        if reason == "content_filter":
                            raise GatewayError("provider_refused", "AI 服务无法完成此内容请求。")
                        if reason == "tool_calls":
                            raise GatewayError("provider_invalid_response", "AI 服务返回了不受支持的工具请求。")
                        message = data["choices"][0]["message"]
                        if message.get("refusal"):
                            raise GatewayError("provider_refused", "AI 服务无法完成此内容请求。")
                        content = message.get("content", "")
                        if not isinstance(content, str):
                            raise ValueError("non-text content")
                        chunks.append(content)
                        usage.update({k: v for k, v in (data.get("usage") or {}).items()
                                      if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                      and isinstance(v, int) and not isinstance(v, bool) and v >= 0})
                        await invoke(on_delta, content)
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        raise GatewayError("provider_invalid_response", "AI 服务响应格式无效。") from None
                else:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        packet = line[5:].strip()
                        if packet == "[DONE]":
                            finished = True
                            break
                        if not packet:
                            continue
                        try:
                            data = json.loads(packet)
                            metadata(data)
                            if data.get("error"):
                                raise GatewayError("provider_stream_error", "AI 流式响应中断。")
                            for choice in data.get("choices", []):
                                delta = choice.get("delta", {})
                                if delta.get("refusal") or choice.get("finish_reason") == "content_filter":
                                    raise GatewayError("provider_refused", "AI 服务无法完成此内容请求。")
                                if choice.get("finish_reason") == "length":
                                    raise GatewayError("provider_truncated", "AI 正文达到长度上限，请精简后重试。")
                                if choice.get("finish_reason") in {"stop", "tool_calls"}:
                                    finished = True
                                content = delta.get("content")
                                if isinstance(content, str) and content:
                                    chunks.append(content)
                                    char_count += len(content)
                                    if char_count > 4 * 1024 * 1024:
                                        raise GatewayError("provider_response_limit", "AI 响应超出安全大小。")
                                    await invoke(on_delta, content)
                            usage.update({k: v for k, v in (data.get("usage") or {}).items()
                                          if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                          and isinstance(v, int) and not isinstance(v, bool) and v >= 0})
                        except (ValueError, TypeError, AttributeError):
                            raise GatewayError("provider_invalid_response", "AI 流式响应格式无效。") from None
                    if chunks and not finished:
                        raise GatewayError("provider_incomplete_stream", "AI 流式响应未完整结束，请稍后重试。")
        finally:
            if owned:
                await client.aclose()
        content = "".join(chunks)
        if not content.strip():
            raise GatewayError("provider_empty_response", "AI 服务返回空正文，请稍后重试。")
        return content, usage
