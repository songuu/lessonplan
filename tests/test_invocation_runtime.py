"""Receipt recovery must never issue a second transport for a paid response."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.ai.gateway import Completion, GatewayError
from app.ai.runtime import LessonRuntime, RuntimeError

INPUT = {"topic": "分数", "generation_mode": "one_click"}
MODULES = [{"module_id": "goals", "module_type": "teaching_objectives"},
           {"module_id": "methods", "module_type": "teaching_methods"}]
BLUEPRINT = {"topic": "分数", "version": 1, "objectives": ["理解平均分"]}


def wrapped(mid, text):
    return f"<!-- MODULE:{mid} -->\n{text}\n<!-- END_MODULE -->"


class Journal:
    def __init__(self):
        self.rows = {}
        self.operations = []

    async def find(self, step_key, attempt):
        return copy.deepcopy(self.rows.get((step_key, attempt)))

    async def reserve(self, **values):
        key = values["step_key"], values["attempt"]
        if key in self.rows:
            return {**copy.deepcopy(self.rows[key]), "claimed": False}
        row = {"id": f"receipt-{len(self.rows) + 1}", "invocation_key": f"key-{len(self.rows) + 1}",
               "status": "in_flight", "capture_epoch": 0, **values}
        self.rows[key] = row
        self.operations.append("reserved")
        return {**copy.deepcopy(row), "claimed": True}

    async def save_response(self, invocation_id, response):
        row = next(row for row in self.rows.values() if row["id"] == invocation_id)
        row.update(status="response_saved", response_body=response.content,
                   response_hash=hashlib.sha256(response.content.encode()).hexdigest(),
                   response_log=copy.deepcopy(response.log))
        self.operations.append("response_saved")
        return copy.deepcopy(row)

    async def mark_unknown(self, invocation_id, error_code):
        row = next(row for row in self.rows.values() if row["id"] == invocation_id)
        row.update(status="outcome_unknown", error_code=error_code)

    async def mark_failed(self, invocation_id, error_code):
        row = next(row for row in self.rows.values() if row["id"] == invocation_id)
        row.update(status="failed", error_code=error_code)


class Gateway:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    async def complete(self, messages, **options):
        self.requests.append((messages, options))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return await response(options)
        if options.get("on_delta"):
            await options["on_delta"](response)
        return Completion(response, {"call_outcome": "success", "total_tokens": 37})


def engine(responses, retry=1):
    gateway = Gateway(responses)
    return LessonRuntime(SimpleNamespace(ai_provider="mock", quality_retry_limit=retry), gateway=gateway), gateway


@pytest.mark.asyncio
async def test_saved_response_replays_before_budget_limit_and_module_checkpoint_failure():
    journal, saved = Journal(), {}

    async def checkpoint(key, payload):
        if key.startswith("module:"):
            assert journal.operations[-1] == "response_saved"
            raise OSError("crash before module commit")
        saved[key] = copy.deepcopy(payload)

    runtime, gateway = engine([wrapped("goals", "学生能解释平均分。")])
    runtime.assets.policy["max_calls"] = 1
    with pytest.raises(OSError, match="before module"):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
    assert len(gateway.requests) == 1
    replay, provider = engine([])
    replay.assets.policy["max_calls"] = 1
    result = await replay.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert result["modules"][0]["teacher_view_content"] == "学生能解释平均分。"
    assert not provider.requests and len(result["calls"]) == 1
    assert saved["budget"]["calls_started"] == 1


@pytest.mark.asyncio
async def test_last_quality_attempt_response_replays_after_budget_commit_crash():
    journal, saved, crash = Journal(), {}, True

    async def checkpoint(key, payload):
        nonlocal crash
        if key == "budget" and journal.operations.count("response_saved") == 2 and crash:
            crash = False
            raise OSError("crash after raw response")
        saved[key] = copy.deepcopy(payload)

    runtime, gateway = engine(["unsafe \\(\\input{secret}\\)", "学生能解释平均分。"])
    with pytest.raises(OSError, match="after raw"):
        await runtime.generate({"topic": "分数", "generation_mode": "structured"}, MODULES[:1],
            blueprint=BLUEPRINT, invocation_store=journal, checkpoint_sink=checkpoint)
    assert len(gateway.requests) == 2
    resumed, provider = engine([])
    result = await resumed.generate({"topic": "分数", "generation_mode": "structured"}, MODULES[:1],
        blueprint=BLUEPRINT, invocation_store=journal, checkpoint_sink=checkpoint, resume=saved)
    assert not provider.requests and len(result["calls"]) == 2
    assert saved["budget"]["calls_started"] == 2
    assert saved["budget"]["attempt_counts"]["module:goals"] == 2


@pytest.mark.asyncio
async def test_batch_replay_keeps_original_members_when_prefix_was_checkpointed():
    journal, saved = Journal(), {}

    async def checkpoint(key, payload):
        if key == "module:methods":
            raise OSError("second module commit crashed")
        saved[key] = copy.deepcopy(payload)

    runtime, _ = engine([wrapped("goals", "学生能解释平均分。") + wrapped("methods", "教师组织折纸讨论。")])
    with pytest.raises(OSError):
        await runtime.generate(INPUT, MODULES, invocation_store=journal, checkpoint_sink=checkpoint)
    assert "module:goals" in saved
    replay, provider = engine([])
    result = await replay.generate(INPUT, MODULES, invocation_store=journal, resume=saved)
    assert not provider.requests
    assert len(result["modules"]) == 2 and len(result["calls"]) == 1


@pytest.mark.asyncio
async def test_stream_callback_failure_drains_and_archives_complete_response():
    journal, chunks = Journal(), []
    text = wrapped("goals", "学生能解释平均分。")

    async def response(options):
        for piece in [text[:25], text[25:]]:
            chunks.append(piece)
            await options["on_delta"](piece)
        return Completion(text, {"call_outcome": "success"})

    async def events(kind, payload):
        if kind == "module_delta":
            raise OSError("notification unavailable")

    runtime, _ = engine([response])
    with pytest.raises(OSError, match="notification"):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, event_sink=events)
    assert len(chunks) == 2
    assert next(iter(journal.rows.values()))["response_body"] == text
    replay, provider = engine([])
    result = await replay.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert not provider.requests and result["modules"]


@pytest.mark.asyncio
async def test_unknown_or_competing_claim_never_starts_another_transport():
    journal = Journal()
    await journal.reserve(step_key="batch:direct", attempt=1, request_hash="x", call_id="call-1",
        call_index=1, module_ids=["goals"], route="one_click")
    runtime, provider = engine([])
    with pytest.raises(RuntimeError) as error:
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert error.value.code == "LLM_OUTCOME_UNKNOWN" and not provider.requests


@pytest.mark.asyncio
async def test_persistent_quality_failure_does_not_recount_batch_on_resume():
    journal, saved = Journal(), {}
    async def checkpoint(key, payload):
        saved[key] = copy.deepcopy(payload)
    bad = r"危险 \(\input{secret}\)"
    runtime, _ = engine([wrapped("goals", bad), bad])
    with pytest.raises(RuntimeError) as error:
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
    assert error.value.code == "module_quality_failed"
    replay, provider = engine([])
    with pytest.raises(RuntimeError):
        await replay.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert not provider.requests


@pytest.mark.asyncio
async def test_one_click_last_repair_response_survives_crash_without_second_quality_charge():
    journal, saved = Journal(), {}
    async def checkpoint(key, payload):
        if key == "budget" and journal.operations.count("response_saved") == 2:
            raise OSError("last repair raw saved")
        saved[key] = copy.deepcopy(payload)
    runtime, _ = engine([wrapped("goals", r"危险 \(\input{secret}\)"), "学生能解释平均分。"])
    with pytest.raises(OSError, match="last repair"):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
    replay, provider = engine([])
    result = await replay.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert not provider.requests and len(result["calls"]) == 2


@pytest.mark.asyncio
async def test_old_batch_cannot_overwrite_successful_repair_before_module_checkpoint():
    journal, saved = Journal(), {}

    async def checkpoint(key, payload):
        if key == "module:goals" and journal.operations.count("response_saved") == 2:
            raise OSError("after validation before module checkpoint")
        saved[key] = copy.deepcopy(payload)

    runtime, _ = engine([wrapped("goals", r"危险 \(\input{secret}\)"), "学生能解释平均分。"])
    with pytest.raises(OSError, match="after validation"):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
    assert saved["budget"]["attempt_counts"]["module:goals"] == 2
    assert saved["budget"]["value"]["failures"] == {}
    assert "module:goals" not in saved
    replay, provider = engine([])
    result = await replay.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert not provider.requests and len(result["calls"]) == 2
    assert result["modules"][0]["teacher_view_content"] == "学生能解释平均分。"
    assert saved["budget"]["calls_started"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["blueprint", "outline", "activity", "polish", "review"])
async def test_every_validated_workflow_reuses_receipt_after_checkpoint_crash(workflow):
    journal, saved = Journal(), {}
    async def checkpoint(key, payload):
        if journal.operations.count("response_saved") and (key == "budget" or key != "blueprint"):
            raise OSError("receipt committed")
        saved[key] = copy.deepcopy(payload)
    if workflow == "blueprint":
        responses = [json.dumps(BLUEPRINT), "学生解释平均分。"]
    elif workflow == "outline":
        responses = [json.dumps([{"stage_id": "a", "title": "观察", "duration_min": 40}])]
    elif workflow == "activity":
        responses = ["学生折纸并解释理由。"]
    elif workflow == "polish":
        responses = ["学生解释平均分。"]
    else:
        responses = [json.dumps({"findings": []})]

    async def execute(runtime, checkpoint=None, resume=None):
        options = dict(invocation_store=journal, checkpoint_sink=checkpoint, resume=resume)
        if workflow == "polish":
            return await runtime.polish({"topic": "分数"}, "学生认识平均分。", "更清晰", **options)
        if workflow == "review":
            return await runtime.review({"topic": "分数"}, MODULES[:1], **options)
        inputs = {"topic": "分数", "generation_mode": "structured"}
        if workflow == "blueprint":
            return await runtime.generate(inputs, MODULES[:1], **options)
        return await runtime.generate(inputs, [{"module_id": "process", "module_type": "teaching_process"}],
            blueprint=BLUEPRINT, stage="outline" if workflow == "outline" else "activities",
            outline=[{"stage_id": "a", "title": "观察", "duration_min": 40}], **options)

    runtime, provider = engine(responses)
    with pytest.raises(OSError, match="receipt committed"):
        await execute(runtime, checkpoint)
    assert len(provider.requests) == 1
    replay, provider = engine(responses[1:] if workflow == "blueprint" else [])
    result = await execute(replay, resume=saved)
    assert len(provider.requests) == (1 if workflow == "blueprint" else 0)
    assert len(result["calls"]) == (2 if workflow == "blueprint" else 1)


@pytest.mark.asyncio
async def test_transport_cancellation_propagates_and_leaves_unknown_intent_blocked():
    journal = Journal()
    runtime, _ = engine([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert next(iter(journal.rows.values()))["status"] == "in_flight"
    replay, provider = engine([])
    with pytest.raises(RuntimeError) as error:
        await replay.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert error.value.code == "LLM_OUTCOME_UNKNOWN" and not provider.requests


@pytest.mark.asyncio
async def test_gateway_ambiguity_marks_unknown_and_never_becomes_quality_retry():
    journal = Journal()
    runtime, provider = engine([GatewayError("provider_timeout", "timeout")])
    with pytest.raises(RuntimeError) as error:
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert error.value.code == "LLM_OUTCOME_UNKNOWN"
    assert len(provider.requests) == 1
    assert next(iter(journal.rows.values()))["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_unexpected_adapter_exception_is_controlled_unknown_and_not_resent():
    journal = Journal()
    runtime, provider = engine([ValueError("secret-adapter-request-details")])
    with pytest.raises(RuntimeError) as error:
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal)
    assert error.value.code == "LLM_OUTCOME_UNKNOWN"
    assert "secret" not in str(error.value) and len(provider.requests) == 1
    assert next(iter(journal.rows.values()))["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_json_fallback_receipt_replays_with_original_batch_membership():
    journal, saved = Journal(), {}
    async def checkpoint(key, payload):
        if key == "budget" and journal.operations.count("response_saved") == 2:
            raise OSError("json raw receipt committed")
        saved[key] = copy.deepcopy(payload)
    good = {"modules": [{"module_id": "goals", "content": "学生解释平均分。"}]}
    runtime, _ = engine(["bad markers", json.dumps(good)])
    with pytest.raises(OSError):
        await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
    replay, provider = engine([])
    result = await replay.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert not provider.requests and len(result["calls"]) == 2
    assert result["modules"][0]["teacher_view_content"] == "学生解释平均分。"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["one_click", "structured"])
async def test_legacy_failure_budget_without_attempt_marker_still_stays_exhausted(mode):
    journal = Journal()
    saved = {"budget": {"value": {"failures": {"module:goals": {
        "code": "module_quality_failed", "codes": ["unsafe_formula"]}}},
        "attempt_counts": {"module:goals": 2}, "calls_started": 2, "calls": []}}
    runtime, provider = engine([])
    with pytest.raises(RuntimeError) as error:
        await runtime.generate({"topic": "分数", "generation_mode": mode}, MODULES[:1],
            blueprint=BLUEPRINT if mode == "structured" else None, invocation_store=journal, resume=saved)
    assert error.value.code == "module_quality_failed" and not provider.requests
