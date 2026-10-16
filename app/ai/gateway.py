"""An asynchronous OpenAI-compatible streaming boundary with auditable attempts."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, calls: list[dict] | None = None):
        super().__init__(message)
        self.code, self.message, self.calls = code, message, calls or []


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
                       mock_content: str | None = None) -> Completion:
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
                async with asyncio.timeout(timeout):
                    content, usage = await self._request(config, messages, route, timeout, on_delta)
                attempts.append({"provider": "backup" if backup else "primary", "model_name": config["model"],
                                 "outcome": "success", "elapsed_ms": round((time.monotonic() - attempt_start) * 1000)})
                log = {"component_name": route, "component_kind": "llm_agent", "llm_provider": "openai_compatible",
                       "model_name": config["model"], "prompt_version": prompt_version,
                       "call_outcome": "success", "fallback_used": backup,
                       "primary_attempt_outcome": "failed" if backup else "success",
                       "elapsed_ms": round((time.monotonic() - started) * 1000), "attempts": attempts,
                       **usage, "is_demo": False}
                return Completion(content, log)
            except (TimeoutError, httpx.TimeoutException):
                last_error = GatewayError("provider_timeout", "AI 服务响应超时，请稍后重试。")
            except httpx.HTTPError:
                last_error = GatewayError("provider_network_error", "AI 服务连接异常，请稍后重试。")
            except GatewayError as exc:
                last_error = exc
            attempts.append({"provider": "backup" if backup else "primary", "model_name": config["model"],
                             "outcome": "failed", "error_code": last_error.code,
                             "elapsed_ms": round((time.monotonic() - attempt_start) * 1000)})
        assert last_error is not None
        failed_log = {"component_name": route, "component_kind": "llm_agent", "llm_provider": "openai_compatible",
                      "model_name": attempts[-1]["model_name"], "prompt_version": prompt_version,
                      "call_outcome": "failed", "fallback_used": len(attempts) > 1,
                      "primary_attempt_outcome": "failed", "error_code": last_error.code,
                      "elapsed_ms": round((time.monotonic() - started) * 1000), "attempts": attempts}
        raise GatewayError(last_error.code, last_error.message, calls=[failed_log])

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
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        try:
            async with client.stream("POST", config["base_url"].rstrip("/") + "/chat/completions",
                                     headers={"Authorization": f"Bearer {config['api_key']}",
                                              "Accept": "text/event-stream"},
                                     json=payload, timeout=timeout) as response:
                if response.status_code >= 400:
                    code = "provider_rate_limited" if response.status_code == 429 else "provider_http_error"
                    raise GatewayError(code, f"AI 服务返回受控错误（HTTP {response.status_code}）。")
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raw = await response.aread()
                    if len(raw) > 4 * 1024 * 1024:
                        raise GatewayError("provider_response_limit", "AI 响应超出安全大小。")
                    try:
                        data = json.loads(raw)
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
                        usage.update({k: int(v) for k, v in data.get("usage", {}).items() if k in usage and isinstance(v, int)})
                        await invoke(on_delta, content)
                    except (ValueError, TypeError, KeyError, IndexError):
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
                            usage.update({k: int(v) for k, v in (data.get("usage") or {}).items()
                                          if k in usage and isinstance(v, int)})
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
