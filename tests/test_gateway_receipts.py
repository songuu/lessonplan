import json
from types import SimpleNamespace

import httpx
import pytest

from app.ai.gateway import GatewayError, LLMGateway


def settings():
    return SimpleNamespace(ai_provider="openai", llm_model="primary", llm_fallback_model="backup",
        llm_api_key="receipt-test-secret", llm_base_url="https://provider.invalid/v1")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "http500", "incomplete", "malformed"])
async def test_strict_outcome_never_falls_back_after_ambiguous_transport(failure):
    requests = []
    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private request body", request=request)
        if failure == "network":
            raise httpx.ConnectError("private provider details", request=request)
        if failure == "http500":
            return httpx.Response(500, json={"error": "private provider details"})
        body = 'data: {"choices":[{"delta":{"content":"已生成内容"}}]}\n\n'
        if failure == "malformed":
            body += "data: malformed\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = LLMGateway(settings(), client=client)
        with pytest.raises(GatewayError) as error:
            await gateway.complete([{"role": "user", "content": "test"}], route="one_click", strict_outcome=True)
    assert len(requests) == 1 and error.value.outcome_unknown is True
    assert error.value.calls[0]["call_outcome"] == "unknown"
    assert "private" not in json.dumps(error.value.calls)


@pytest.mark.asyncio
async def test_completion_retains_bounded_provider_id_and_finish_reason():
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
        'data: {"id":"chatcmpl-receipt_123","choices":[{"delta":{"content":"完整响应"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n').encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await LLMGateway(settings(), client=client).complete([], route="structured", strict_outcome=True)
    assert result.log["provider_request_id"] == "chatcmpl-receipt_123"
    assert result.log["finish_reason"] == "stop"
    assert "secret" not in json.dumps(result.log)


@pytest.mark.asyncio
async def test_provider_metadata_rejects_unbounded_or_credential_like_values():
    response = httpx.Response(200, headers={"content-type": "application/json"}, json={
        "id": "receipt-test-secret", "choices": [{"finish_reason": "stop", "message": {"content": "完整响应"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await LLMGateway(settings(), client=client).complete([], route="structured", strict_outcome=True)
    assert "provider_request_id" not in result.log


@pytest.mark.asyncio
async def test_explicit_rate_limit_can_use_backup_without_unknown_outcome():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"id": "success-id", "choices": [
            {"finish_reason": "stop", "message": {"content": "完整响应"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await LLMGateway(settings(), client=client).complete([], route="structured", strict_outcome=True)
    assert len(requests) == 2 and result.log["fallback_used"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("usage, expected", [
    (None, {}),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
     {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ({"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11},
     {"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11}),
    ({"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "4",
      "provider_request_id": 0, "finish_reason": 0}, {}),
])
async def test_reported_usage_is_distinct_from_missing_usage(streaming, usage, expected):
    choice = {"finish_reason": "stop",
              "delta" if streaming else "message": {"content": "完整响应"}}
    data = {"id": "safe-request-id", "choices": [choice]}
    if usage is not None:
        data["usage"] = usage
    response = (httpx.Response(200, headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode())
        if streaming else httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await LLMGateway(settings(), client=client).complete([], route="structured", strict_outcome=True)
    token_keys = {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert {key: value for key, value in result.log.items() if key in token_keys} == expected
    assert result.log["provider_request_id"] == "safe-request-id"
    assert result.log["finish_reason"] == "stop"
