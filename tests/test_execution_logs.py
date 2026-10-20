"""Local execution diagnostics survive restarts without joining business transactions."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Draft, ExecutionLog, Run
from app.services.execution_logs import ExecutionLogWriter
from tests.test_execution_recovery import frozen_run


def log_payload(run_id, **changes):
    trace_id = str(uuid4())
    return {
        "event_id": str(uuid4()), "phase": "end", "span_id": str(uuid4()),
        "trace_id": trace_id, "parent_span_id": trace_id,
        "business_run_id": run_id, "execution_epoch": 0,
        "name": "llm.request", "run_type": "llm", "level": "info", "status": "completed",
        "started_at": datetime.now(UTC).isoformat(), "ended_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": 42, "metadata": {"model_name": "test-model", "total_tokens": 12}, **changes,
    }


async def test_writer_durable_snapshot_sanitization_and_restart(client):
    _, item = await frozen_run(client)
    rid = item["run_id"]
    writer = ExecutionLogWriter(client.app.state.db.session_factory)
    payload = log_payload(rid, metadata={
        "model_name": "test-model", "total_tokens": 12, "usage_known": True,
        "prompt": "PRIVATE_PROMPT", "api_key": "sk-private", "error": "PRIVATE_TRACEBACK",
        "validation_codes": ["MISSING_OBJECTIVES"], "module_id": {"body": "PRIVATE_BODY"},
    })
    writer.submit(payload)
    payload["metadata"]["total_tokens"] = 999
    await writer.start()
    await writer.flush()
    await writer.close()
    restarted = ExecutionLogWriter(client.app.state.db.session_factory)
    async with client.app.state.db.session_factory() as session:
        rows = (await session.scalars(select(ExecutionLog).where(ExecutionLog.run_id == rid))).all()
        assert len(rows) == 1
        assert rows[0].metadata_json == {"model_name": "test-model", "total_tokens": 12,
            "usage_known": True, "validation_codes": ["MISSING_OBJECTIVES"]}
        assert rows[0].started_at.tzinfo == UTC and rows[0].elapsed_ms == 42
    restarted.submit(log_payload(rid))
    await restarted.start()
    await restarted.close()
    async with client.app.state.db.session_factory() as session:
        sequences = (await session.scalars(select(ExecutionLog.sequence).where(
            ExecutionLog.run_id == rid).order_by(ExecutionLog.sequence))).all()
        assert len(sequences) == 2 and sequences[1] > sequences[0]


async def test_duplicates_missing_runs_and_old_epoch_are_diagnostic_only(client):
    _, item = await frozen_run(client)
    rid = item["run_id"]
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, rid)
        run.execution_epoch = 3
        await session.commit()
    writer = ExecutionLogWriter(client.app.state.db.session_factory)
    payload = log_payload(rid, status="lease_lost", level="warning")
    writer.submit(payload)
    writer.submit(payload)
    writer.submit(log_payload(str(uuid4())))
    writer.submit(log_payload(rid))
    await writer.start()
    await writer.close()
    async with client.app.state.db.session_factory() as session:
        rows = (await session.scalars(select(ExecutionLog).where(ExecutionLog.run_id == rid))).all()
        assert len(rows) == 2 and all(row.execution_epoch == 0 for row in rows)
        run = await session.get(Run, rid)
        assert run.execution_epoch == 3 and run.status == "queued" and run.sequence == 0
    assert writer.status()["written"] == 2


async def test_soft_deleted_draft_is_not_logged(client):
    _, item = await frozen_run(client)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        draft = await session.get(Draft, run.draft_id)
        draft.deleted_at = datetime.now(UTC)
        await session.commit()
    writer = ExecutionLogWriter(client.app.state.db.session_factory)
    writer.submit(log_payload(item["run_id"]))
    await writer.start()
    await writer.close()
    async with client.app.state.db.session_factory() as session:
        assert not await session.scalar(select(ExecutionLog.sequence))


async def test_queue_overflow_invalid_payload_and_closed_writer_never_raise(client):
    _, item = await frozen_run(client)
    writer = ExecutionLogWriter(client.app.state.db.session_factory, max_queue=1)
    writer.submit(None)
    writer.submit(log_payload(item["run_id"], span_id="not-a-uuid"))
    writer.submit(log_payload(item["run_id"]))
    writer.submit(log_payload(item["run_id"]))
    assert writer.status()["queued"] == 1 and writer.status()["dropped"] == 3
    await writer.start()
    await writer.close()
    writer.submit(log_payload(item["run_id"]))
    assert writer.status()["dropped"] == 4


async def test_failed_database_is_isolated_and_next_batch_recovers(client):
    _, item = await frozen_run(client)
    calls = 0
    def flaky_factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("PRIVATE_DATABASE_CREDENTIALS")
        return client.app.state.db.session_factory()
    writer = ExecutionLogWriter(flaky_factory)
    await writer.start()
    writer.submit(log_payload(item["run_id"]))
    await writer.flush()
    assert writer.status()["errors"] == 1
    writer.submit(log_payload(item["run_id"]))
    await writer.close()
    assert writer.status()["written"] == 1
    assert "PRIVATE_DATABASE_CREDENTIALS" not in str(writer.status())


async def test_stalled_database_shutdown_is_bounded_and_reports_loss():
    class StalledSession:
        async def __aenter__(self):
            await asyncio.Future()

        async def __aexit__(self, *args):
            pass

    writer = ExecutionLogWriter(StalledSession)
    await writer.start()
    writer.submit(log_payload(str(uuid4())))
    await asyncio.wait_for(writer.close(timeout=0.01), timeout=0.5)
    assert writer.status()["dropped"] == 1
    assert writer.status()["flush_timeouts"] == 1
    assert writer.status()["running"] is False


async def test_fractional_timings_and_receipt_replay_flags_survive_storage(client):
    _, item = await frozen_run(client)
    writer = ExecutionLogWriter(client.app.state.db.session_factory)
    writer.submit(log_payload(item["run_id"], elapsed_ms=42.75, metadata={
        "first_token_ms": 1.75, "receipt_reused": True, "receipt_replayed": True,
        "prompt": "PRIVATE_PROMPT", "elapsed_ms": float("nan"),
    }))
    await writer.start()
    await writer.close()
    async with client.app.state.db.session_factory() as session:
        row = await session.scalar(select(ExecutionLog).where(ExecutionLog.run_id == item["run_id"]))
        assert row is not None and row.elapsed_ms == 42
        assert row.metadata_json == {"first_token_ms": 1, "receipt_reused": True, "receipt_replayed": True}
