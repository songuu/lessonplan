"""Opt-in cursor ordering checks in a dedicated disposable loopback PostgreSQL database."""

import asyncio

from sqlalchemy import event, select, text

from app.db.models import ExecutionLog
from app.services.execution_logs import ExecutionLogWriter
from tests.test_dispatch_postgres import isolated_app, pending_run
from tests.test_execution_logs import log_payload


async def test_postgres_execution_cursor_waits_for_earlier_commit():
    app = isolated_app()
    allocated = asyncio.Event()
    release_commit = asyncio.Event()
    first_sequence = []
    writers = []

    def delayed_session():
        session = app.state.db.session_factory()

        @event.listens_for(session.sync_session, "before_commit", once=True)
        def delay_after_sequence(sync_session):
            rows = [row for row in sync_session.new if isinstance(row, ExecutionLog)]
            assert len(rows) == 1
            # The sequence is allocated by PostgreSQL before the transaction commits.
            sync_session.flush()
            first_sequence.append(rows[0].sequence)

            async def hold_commit(_):
                allocated.set()
                await release_commit.wait()

            sync_session.connection().connection.dbapi_connection.run_async(hold_commit)

        return session

    try:
        run_id = await pending_run(app)
        first = ExecutionLogWriter(delayed_session)
        second = ExecutionLogWriter(app.state.db.session_factory)
        writers.extend((first, second))
        await asyncio.gather(first.start(), second.start())
        first_event, second_event = log_payload(run_id), log_payload(run_id)
        first.submit(first_event)
        await asyncio.wait_for(allocated.wait(), timeout=2)
        assert len(first_sequence) == 1 and first_sequence[0] > 0

        second.submit(second_event)
        # Stay comfortably below the writer's 500 ms advisory-lock timeout.
        assert not await second.flush(timeout=0.05), "A later writer committed past an uncommitted cursor"
        async with app.state.db.session_factory() as session:
            assert not (await session.scalars(select(ExecutionLog.sequence).where(
                ExecutionLog.run_id == run_id))).all()
            assert await session.scalar(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted
                      AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                )
            """)), "The second process must wait on the database lock before allocating its cursor"

        release_commit.set()
        assert all(await asyncio.gather(first.flush(timeout=2), second.flush(timeout=2)))
        assert first.status()["written"] == second.status()["written"] == 1
        assert first.status()["errors"] == second.status()["errors"] == 0
        async with app.state.db.session_factory() as session:
            page_one = (await session.scalars(select(ExecutionLog).where(ExecutionLog.run_id == run_id,
                ExecutionLog.sequence > 0).order_by(ExecutionLog.sequence).limit(1))).all()
            assert len(page_one) == 1 and page_one[0].event_id == first_event["event_id"]
            cursor = page_one[0].sequence
            assert cursor == first_sequence[0]
            page_two = (await session.scalars(select(ExecutionLog).where(ExecutionLog.run_id == run_id,
                ExecutionLog.sequence > cursor).order_by(ExecutionLog.sequence).limit(1))).all()
            assert len(page_two) == 1 and page_two[0].event_id == second_event["event_id"]
            assert page_two[0].sequence > cursor
    finally:
        release_commit.set()
        await asyncio.gather(*(writer.close(timeout=2) for writer in writers))
        await app.state.db.engine.dispose()
