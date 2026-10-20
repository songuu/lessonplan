"""Crash windows exercise the real journal, runtime, leases and lesson transactions."""
import asyncio

import pytest
from sqlalchemy import func, select

from app.ai.gateway import Completion, invoke
from app.db.models import Event, LLMInvocation, ModuleVersion, RecoveryAlert, Run, RunCheckpoint
from app.services.invocations import InvocationJournal
from app.services.lessons import WorkflowExecutor
from tests.test_execution_recovery import frozen_run


class CountingGateway:
    def __init__(self, *, interrupted=False):
        self.requests = 0
        self.interrupted = interrupted

    async def complete(self, messages, **options):
        self.requests += 1
        if self.interrupted:
            raise asyncio.CancelledError()
        content = options["mock_content"]
        await invoke(options.get("on_delta"), content)
        return Completion(content, {"call_outcome": "success", "model_name": "synthetic-provider",
            "llm_provider": "synthetic", "prompt_tokens": 19, "completion_tokens": 23, "total_tokens": 42})


@pytest.mark.parametrize("window", ["after_response", "before_module", "before_business_commit", "commit_ack_lost"])
async def test_recovery_of_saved_response_never_regenerates_or_duplicates_versions(client, monkeypatch, window):
    from app.services import lessons

    _, item = await frozen_run(client)
    rid = item["run_id"]
    gateway = CountingGateway()
    client.app.state.runtime.gateway = gateway
    # Replay must remain possible even when the only paid call consumed the whole allowance.
    client.app.state.runtime.assets.policy["max_calls"] = 1
    triggered = False
    original_response = InvocationJournal.save_response
    original_checkpoint = WorkflowExecutor.save_checkpoint
    original_finish = lessons.finish

    async def response(self, invocation_id, completion):
        nonlocal triggered
        saved = await original_response(self, invocation_id, completion)
        if window == "after_response" and not triggered:
            triggered = True
            raise asyncio.CancelledError()
        return saved

    async def checkpoint(self, run_id, key, payload, fingerprint):
        nonlocal triggered
        if window == "before_module" and key.startswith("module:") and not triggered:
            triggered = True
            raise asyncio.CancelledError()
        return await original_checkpoint(self, run_id, key, payload, fingerprint)

    async def finish(session, run, status, **options):
        nonlocal triggered
        result = await original_finish(session, run, status, **options)
        if run.id == rid and status == "completed" and window in {"before_business_commit", "commit_ack_lost"} and not triggered:
            triggered = True
            if window == "commit_ack_lost":
                # The server committed; the process dies before its caller sees that success.
                await session.commit()
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(InvocationJournal, "save_response", response)
    monkeypatch.setattr(WorkflowExecutor, "save_checkpoint", checkpoint)
    monkeypatch.setattr(lessons, "finish", finish)
    with pytest.raises(asyncio.CancelledError):
        await client.app.state.dispatcher._execute(rid, 0)
    assert triggered and gateway.requests == 1
    async with client.app.state.db.session_factory() as session:
        receipt = await session.scalar(select(LLMInvocation).where(LLMInvocation.run_id == rid))
        assert receipt.response_body and receipt.response_hash
        assert receipt.status == ("applied" if window == "commit_ack_lost" else "response_saved")
        before = await session.scalar(select(func.count()).select_from(ModuleVersion)
            .where(ModuleVersion.draft_id == item["draft_id"]))
        assert before == (7 if window == "commit_ack_lost" else 0)
    if window == "commit_ack_lost":
        assert not await client.app.state.dispatcher.recover_run(rid, expected_epoch=0)
        await client.app.state.dispatcher._execute(rid, 0)
    else:
        assert await client.app.state.dispatcher.recover_run(rid, expected_epoch=0)
        await client.app.state.dispatcher._execute(rid, 1)
    assert gateway.requests == 1
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, rid)
        receipt = await session.scalar(select(LLMInvocation).where(LLMInvocation.run_id == rid))
        budget = await session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == rid,
            RunCheckpoint.checkpoint_key == "budget"))
        assert run.status == "completed" and receipt.status == "applied"
        assert budget.payload["calls_started"] == 1 and len(budget.payload["calls"]) == 1
        assert await session.scalar(select(func.count()).select_from(ModuleVersion)
            .where(ModuleVersion.draft_id == item["draft_id"])) == 7
        assert await session.scalar(select(func.count()).select_from(Event).where(Event.run_id == rid,
            Event.event_type == "run_completed")) == 1
        sequences = (await session.scalars(select(Event.sequence).where(Event.run_id == rid)
            .order_by(Event.sequence))).all()
        assert sequences == list(range(1, run.sequence + 1))


async def test_unknown_transport_after_worker_crash_stops_without_second_model_request(client):
    _, item = await frozen_run(client)
    rid = item["run_id"]
    gateway = CountingGateway(interrupted=True)
    client.app.state.runtime.gateway = gateway
    with pytest.raises(asyncio.CancelledError):
        await client.app.state.dispatcher._execute(rid, 0)
    assert await client.app.state.dispatcher.recover_run(rid, expected_epoch=0)
    await client.app.state.dispatcher._execute(rid, 1)
    assert gateway.requests == 1
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, rid)
        row = await session.scalar(select(LLMInvocation).where(LLMInvocation.run_id == rid))
        alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == rid,
            RecoveryAlert.code == "LLM_OUTCOME_UNKNOWN"))
        assert run.status == "failed" and run.error_code == "LLM_OUTCOME_UNKNOWN"
        assert row.status == "outcome_unknown" and row.response_body is None
        assert alert.status == "open"
