"""Durable model receipts remain separate from fenced lesson transactions."""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.ai.gateway import Completion
from app.core.errors import AppError
from app.db.models import LLMInvocation, ModuleVersion, Run
from app.services.invocations import InvocationJournal
from app.services.lessons import ExecutionLease, LeaseLost, WorkflowExecutor, database_now, uid
from tests.conftest import login
from tests.test_execution_recovery import expire
from tests.test_workflows import create


async def journal(client, fingerprint="frozen-input"):
    await client.app.state.dispatcher.stop_maintenance()
    headers = await login(client, email=f"journal-{uid()}@example.test")
    original = client.app.state.dispatcher.submit
    async def frozen(*args, **kwargs):
        pass
    client.app.state.dispatcher.submit = frozen
    try:
        item = await create(client, headers)
    finally:
        client.app.state.dispatcher.submit = original
    executor = WorkflowExecutor(client.app)
    assert await executor.claim(item["run_id"], 0)
    return item, executor, InvocationJournal(executor, item["run_id"], fingerprint)


async def reserve(store, **changes):
    return await store.reserve(**{"step_key": "one_click:markdown", "attempt": 1,
        "request_hash": "a" * 64, "call_id": "call-1", "call_index": 1,
        "module_ids": ["goals", "methods"], "route": "one_click", **changes})


async def test_raw_receipt_commits_before_business_and_remains_immutable(client):
    item, _, store = await journal(client)
    claim = await reserve(store)
    assert claim["claimed"] and claim["status"] == "in_flight"
    duplicate = await reserve(store)
    assert not duplicate["claimed"] and duplicate["id"] == claim["id"]
    result = Completion("完整原始响应", {"model_name": "test-model", "call_outcome": "success",
        "total_tokens": 37, "prompt": "must-not-leak", "api_key": "secret"})
    saved = await store.save_response(claim["id"], result)
    assert saved["status"] == "response_saved" and saved["response_body"] == result.content
    assert saved["response_hash"] and saved["response_log"]["total_tokens"] == 37
    assert "prompt" not in saved["response_log"] and "api_key" not in saved["response_log"]
    assert (await store.save_response(claim["id"], result))["response_hash"] == saved["response_hash"]
    with pytest.raises(AppError) as conflict:
        await store.save_response(claim["id"], Completion("另一个响应", {}))
    assert conflict.value.code == "LLM_RESPONSE_CONFLICT"
    async with client.app.state.db.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ModuleVersion)
            .where(ModuleVersion.draft_id == item["draft_id"])) == 0
        row = await session.get(LLMInvocation, claim["id"])
        assert row.response_body == "完整原始响应"


async def test_recovered_epoch_reuses_same_receipt_and_preserves_call_identity(client):
    item, _, original = await journal(client)
    claim = await reserve(original)
    await original.save_response(claim["id"], Completion("响应已经落库", {"call_outcome": "success"}))
    await expire(client, item["run_id"])
    assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    successor = WorkflowExecutor(client.app)
    assert await successor.claim(item["run_id"], 1)
    replay = await InvocationJournal(successor, item["run_id"], "frozen-input").find("one_click:markdown", 1)
    assert replay["id"] == claim["id"] and replay["call_id"] == "call-1"
    assert replay["call_index"] == 1 and replay["capture_epoch"] == 0
    assert replay["response_body"] == "响应已经落库"


async def test_unknown_does_not_grant_another_transport_and_late_response_can_be_archived(client):
    item, old, original = await journal(client)
    claim = await reserve(original)
    await expire(client, item["run_id"])
    assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    successor = WorkflowExecutor(client.app)
    assert await successor.claim(item["run_id"], 1)
    recovered = InvocationJournal(successor, item["run_id"], "frozen-input")
    unknown = await recovered.find("one_click:markdown", 1)
    assert unknown["status"] == "outcome_unknown"
    assert not (await reserve(recovered))["claimed"]
    with pytest.raises(LeaseLost):
        await recovered.save_response(claim["id"], Completion("冒充旧传输者", {}))
    await original.save_response(claim["id"], Completion("旧传输者的完整迟到响应", {}))
    assert (await recovered.find("one_click:markdown", 1))["response_body"] == "旧传输者的完整迟到响应"
    with pytest.raises(LeaseLost):
        await old.save_checkpoint(item["run_id"], "module:goals", {}, "frozen-input")


async def test_context_change_and_wrong_run_never_reuse_or_overwrite_response(client):
    item, executor, store = await journal(client)
    claim = await reserve(store)
    changed = InvocationJournal(executor, item["run_id"], "changed-input")
    with pytest.raises(AppError) as mismatch:
        await changed.find("one_click:markdown", 1)
    assert mismatch.value.code == "LLM_INVOCATION_CONTEXT_CHANGED"
    with pytest.raises(AppError):
        await reserve(store, request_hash="b" * 64)
    other_item, other_executor, other = await journal(client)
    assert other_item["run_id"] != item["run_id"]
    with pytest.raises(LeaseLost):
        await other.save_response(claim["id"], Completion("wrong task", {}))
    assert await other.find("one_click:markdown", 1) is None
    # A fabricated current epoch does not authorize writing the original transport's receipt.
    other_executor.lease = ExecutionLease(item["run_id"], 0, "wrong-owner")
    with pytest.raises(LeaseLost):
        await InvocationJournal(other_executor, item["run_id"], "frozen-input").mark_unknown(claim["id"], "PROVIDER_TIMEOUT")


async def test_applied_receipts_share_business_transaction_and_rollback(client):
    item, executor, store = await journal(client)
    claim = await reserve(store)
    await store.save_response(claim["id"], Completion("complete", {}))
    async with client.app.state.db.session_factory() as session:
        await executor.lock_execution(session, item["run_id"])
        await store.mark_applied(session)
        await session.rollback()
    assert (await store.find("one_click:markdown", 1))["status"] == "response_saved"
    async with client.app.state.db.session_factory() as session:
        await executor.lock_execution(session, item["run_id"])
        await store.mark_applied(session)
        await session.commit()
    assert (await store.find("one_click:markdown", 1))["status"] == "applied"


async def test_reserve_concurrency_grants_one_transport_and_cancel_blocks_new_calls(client):
    item, _, store = await journal(client)
    claims = await asyncio.gather(reserve(store), reserve(store))
    assert sum(record["claimed"] for record in claims) == 1
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        run.cancel_requested = True
        run.lease_expires_at = await database_now(session) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(LeaseLost):
        await reserve(store, step_key="module:another")
