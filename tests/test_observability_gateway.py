"""Real HTTP boundary spans count transport attempts and exclude user content."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.ai.gateway import GatewayError, LLMGateway
from app.ai.runtime import LessonRuntime
from app.observability import create_observer


def settings(**values):
    return SimpleNamespace(ai_provider="openai", llm_model="test-model", llm_api_key="test-provider-credential",
        llm_base_url="https://provider.invalid/v1", obs_deployment_id="test", **values)


def reply(usage=None):
    value = {"choices": [{"message": {"content": "PRIVATE_MODEL_BODY"}, "finish_reason": "stop"}]}
    if usage is not None:
        value["usage"] = usage
    return httpx.Response(200, json=value)


async def test_transport_span_has_usage_first_token_and_no_private_payload():
    events = []
    config = settings()
    observer = create_observer(config, local_sink=events.append)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply({
        "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}))) as client:
        with observer.execution(str(uuid4()), 0):
            result = await LLMGateway(config, client=client).complete(
                [{"role": "user", "content": "PRIVATE_TEACHER_PROMPT"}], route="one_click")
    spans = [event for event in events if event["run_type"] == "llm" and event["phase"] == "end"]
    assert len(spans) == 1
    assert spans[0]["metadata"]["usage_known"] is True
    assert spans[0]["metadata"]["total_tokens"] == 20
    assert spans[0]["metadata"]["first_token_ms"] >= 0
    assert result.log["usage_reported"] is True
    serialized = json.dumps(events)
    assert "PRIVATE_MODEL_BODY" not in serialized and "PRIVATE_TEACHER_PROMPT" not in serialized
    assert config.llm_api_key not in serialized


async def test_missing_usage_remains_unknown_instead_of_zero():
    events = []
    config = settings()
    observer = create_observer(config, local_sink=events.append)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply())) as client:
        with observer.execution(str(uuid4()), 0):
            result = await LLMGateway(config, client=client).complete([], route="one_click")
    model = next(event for event in events if event["run_type"] == "llm" and event["phase"] == "end")
    assert model["metadata"]["usage_known"] is False
    assert "input_tokens" not in model["metadata"]
    assert result.log["usage_reported"] is False


async def test_partial_provider_usage_keeps_known_input_and_leaves_total_unknown():
    events = []
    config = settings()
    observer = create_observer(config, local_sink=events.append)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply({"prompt_tokens": 12}))) as client:
        with observer.execution(str(uuid4()), 0):
            await LLMGateway(config, client=client).complete([], route="one_click")
    model = next(event for event in events if event["run_type"] == "llm" and event["phase"] == "end")
    assert model["metadata"]["input_tokens"] == 12
    assert "output_tokens" not in model["metadata"] and "total_tokens" not in model["metadata"]


async def test_primary_rate_limit_and_backup_are_separate_actual_spans():
    events, requests = [], []
    config = settings(llm_fallback_model="backup")
    observer = create_observer(config, local_sink=events.append)

    def handler(request):
        requests.append(request)
        return httpx.Response(429) if len(requests) == 1 else reply({"prompt_tokens": 2, "completion_tokens": 3})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with observer.execution(str(uuid4()), 0):
            await LLMGateway(config, client=client).complete([], route="one_click", strict_outcome=True)
    spans = [event for event in events if event["run_type"] == "llm" and event["phase"] == "end"]
    assert len(requests) == len(spans) == 2
    assert [event["metadata"]["attempt_role"] for event in spans] == ["primary", "backup"]
    assert [event["status"] for event in spans] == ["failed", "completed"]


async def test_unknown_failure_does_not_create_backup_span_or_leak_exception():
    events = []
    config = settings(llm_fallback_model="backup")
    observer = create_observer(config, local_sink=events.append)

    def handler(request):
        raise httpx.ReadTimeout("PRIVATE_PROVIDER_EXCEPTION", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with observer.execution(str(uuid4()), 0):
            with pytest.raises(GatewayError):
                await LLMGateway(config, client=client).complete([], route="one_click", strict_outcome=True)
    spans = [event for event in events if event["run_type"] == "llm" and event["phase"] == "end"]
    assert len(spans) == 1 and spans[0]["metadata"]["transport_outcome"] == "unknown"
    assert "PRIVATE_PROVIDER_EXCEPTION" not in json.dumps(events)


@pytest.mark.parametrize("body", [
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    'data: {"error":{"message":"PRIVATE_PROVIDER_EXCEPTION"}}\n\n',
    'data: malformed\n\n',
])
async def test_strict_stream_ambiguity_matches_the_receipt_outcome(body):
    events = []
    config = settings(llm_fallback_model="backup")
    observer = create_observer(config, local_sink=events.append)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()))) as client:
        with observer.execution(str(uuid4()), 0):
            with pytest.raises(GatewayError) as result:
                await LLMGateway(config, client=client).complete([], route="one_click", strict_outcome=True)
    models = [event for event in events if event["run_type"] == "llm" and event["phase"] == "end"]
    assert result.value.outcome_unknown is True
    assert len(models) == 1 and models[0]["metadata"]["transport_outcome"] == "unknown"
    assert "PRIVATE_PROVIDER_EXCEPTION" not in json.dumps(events)


async def test_receipt_replay_after_crash_does_not_add_generation_or_usage():
    from tests.test_invocation_runtime import INPUT, MODULES, Journal, wrapped

    events, saved, requests = [], {}, []
    config = settings(quality_retry_limit=1)
    observer = create_observer(config, local_sink=events.append)
    journal, run_id = Journal(), str(uuid4())

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": wrapped("goals", "学生能解释平均分。")}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8}})

    async def checkpoint(key, payload):
        if key.startswith("module:"):
            raise OSError("synthetic checkpoint failure")
        saved[key] = copy.deepcopy(payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = LLMGateway(config, client=client)
        engine = LessonRuntime(config, gateway=gateway)
        with observer.execution(run_id, 0):
            with pytest.raises(OSError):
                await engine.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
        with observer.execution(run_id, 1):
            result = await engine.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert result["modules"][0]["teacher_view_content"] == "学生能解释平均分。"
    requests_logged = [event for event in events if event["run_type"] == "llm" and event["phase"] == "end"]
    assert len(requests) == len(requests_logged) == 1
    assert requests_logged[0]["metadata"]["total_tokens"] == 20
    replay = next(event for event in events if event["name"] == "receipt.replay" and event["phase"] == "end")
    assert replay["execution_epoch"] == 1 and replay["metadata"]["receipt_reused"] is True
    assert "total_tokens" not in replay["metadata"]
