"""Opt-in PostgreSQL concurrency checks, restricted to a separately created disposable database."""
import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.models import Base, Draft, Outbox, RecoveryAlert, Run, User
from app.factory import create_app
from app.services.lessons import database_now, uid
from app.services.recovery import signal_alert


def isolated_app():
    source = os.environ.get("LESSON_RECOVERY_TEST_DATABASE_URL")
    if not source:
        pytest.skip("A dedicated local recovery verification database is required")
    url = make_url(source)
    if (url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1" or url.query
            or not url.database or not url.database.startswith("lesson_recovery_verify_")):
        raise ValueError("Recovery verification requires a dedicated loopback lesson_recovery_verify_ database")
    return create_app(Settings(_env_file=None, environment="test", database_url=source,
        task_queue_mode="arq", ai_provider="mock", storage_backend="local"))


async def pending_run(app):
    async with app.state.db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with app.state.db.session_factory() as session:
        user = User(id=uid(), email=f"concurrency-{uid()}@example.test", password_hash="unused")
        session.add(user)
        await session.flush()
        draft = Draft(id=uid(), user_id=user.id, tenant_id=user.tenant_id, request_id=uid(),
            input_hash="recovery-verification", topic="合成并发验证")
        session.add(draft)
        await session.flush()
        run = Run(id=uid(), draft_id=draft.id, user_id=user.id, tenant_id=user.tenant_id,
            workflow_type="one_click", idempotency_key=uid(), input_hash="verification",
            status="queued", queue_name="lesson:one-click", job_id=uid(), payload={})
        session.add(run)
        await session.flush()
        session.add(Outbox(run_id=run.id, event_type="dispatch_run", status="pending",
            payload={"run_id": run.id, "execution_epoch": 0}, available_at=await database_now(session)))
        await session.commit()
        return run.id


async def test_postgres_concurrent_dispatch_claim_and_stale_receipt_fencing():
    app = isolated_app()
    try:
        rid = await pending_run(app)
        dispatcher = app.state.dispatcher
        claims = await asyncio.gather(dispatcher._reserve(rid), dispatcher._reserve(rid))
        assert sum(claim is not None for claim in claims) == 1
        first = next(claim for claim in claims if claim is not None)
        assert first["attempts"] == 1
        await dispatcher._complete(first, "error", "DISPATCH_UNAVAILABLE")
        assert await dispatcher._reserve(rid) is None
        async with app.state.db.session_factory() as session:
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == rid))
            intent.available_at = await database_now(session) - timedelta(seconds=1)
            await session.commit()
        second = await dispatcher._reserve(rid)
        assert second["attempts"] == 2
        await dispatcher._complete(first, "published", None)
        async with app.state.db.session_factory() as session:
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == rid))
            assert intent.status == "dispatching" and intent.attempts == 2
        await dispatcher._complete(second, "published", None)
        async with app.state.db.session_factory() as session:
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == rid))
            alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == rid))
            assert intent.status == "published" and intent.attempts == 2
            assert alert.status == "resolved" and alert.occurrence_count == 1
    finally:
        await app.state.db.engine.dispose()


async def test_postgres_alert_upsert_deduplicates_concurrent_signals():
    app = isolated_app()
    try:
        rid = await pending_run(app)
        async def signal():
            async with app.state.db.session_factory() as session:
                run = await session.get(Run, rid)
                await signal_alert(session, "DISPATCH_RETRY", "safe", run=run, metadata={"attempts": 1})
                await session.commit()
        await asyncio.gather(signal(), signal())
        async with app.state.db.session_factory() as session:
            records = (await session.scalars(select(RecoveryAlert).where(RecoveryAlert.run_id == rid))).all()
            assert len(records) == 1 and records[0].occurrence_count == 2
    finally:
        await app.state.db.engine.dispose()
