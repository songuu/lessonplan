"""Opt-in, isolated PostgreSQL locking and receipt transaction checks."""
import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app.ai.gateway import Completion
from app.db.models import LLMInvocation, Run
from app.services.invocations import InvocationJournal
from app.services.lessons import WorkflowExecutor, database_now
from tests.test_dispatch_postgres import isolated_app, pending_run


async def test_postgres_receipt_concurrent_reservation_and_immutable_replies():
    app = isolated_app()
    try:
        rid = await pending_run(app)
        executor = WorkflowExecutor(app)
        assert await executor.claim(rid, 0)
        journal = InvocationJournal(executor, rid, "frozen-context")
        async def reserve():
            return await journal.reserve("batch:direct", 1, "a" * 64, "call-1", 1, [], "one_click")
        claims = await asyncio.gather(reserve(), reserve())
        assert sum(row["claimed"] for row in claims) == 1
        response = Completion("durable paid response", {"total_tokens": 42})
        replies = await asyncio.gather(*(journal.save_response(row["id"], response) for row in claims))
        assert replies[0]["response_hash"] == replies[1]["response_hash"]
        async with app.state.db.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(LLMInvocation)
                .where(LLMInvocation.run_id == rid)) == 1
    finally:
        await app.state.db.engine.dispose()


async def test_postgres_recovered_raw_receipt_and_business_rollback():
    app = isolated_app()
    try:
        rid = await pending_run(app)
        executor = WorkflowExecutor(app)
        assert await executor.claim(rid, 0)
        journal = InvocationJournal(executor, rid, "frozen-context")
        claim = await journal.reserve("module:goals", 1, "a" * 64, "call-1", 1, ["goals"], "structured")
        await journal.save_response(claim["id"], Completion("raw response", {}))
        async with app.state.db.session_factory() as session:
            await executor.lock_execution(session, rid)
            await journal.mark_applied(session)
            await session.rollback()
        async with app.state.db.session_factory() as session:
            row = await session.get(LLMInvocation, claim["id"])
            assert row.status == "response_saved"
            run = await session.get(Run, rid)
            run.lease_expires_at = await database_now(session) - timedelta(seconds=1)
            await session.commit()
        assert await app.state.dispatcher.recover_run(rid, expected_epoch=0)
        successor = WorkflowExecutor(app)
        assert await successor.claim(rid, 1)
        cached = await InvocationJournal(successor, rid, "frozen-context").find("module:goals", 1)
        assert cached["id"] == claim["id"] and cached["response_body"] == "raw response"
        assert cached["call_index"] == 1 and cached["capture_epoch"] == 0
    finally:
        await app.state.db.engine.dispose()
