import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.core.errors import AppError
from app.db.models import (
    CallLog,
    Draft,
    Event,
    ModuleVersion,
    Outbox,
    PolishMessage,
    PolishSession,
    QualityResult,
    Run,
    RunCheckpoint,
)
from app.services.lessons import LeaseLost, WorkflowExecutor, database_now, uid
from tests.conftest import login
from tests.test_workflows import create, wait_run


async def frozen_run(client):
    # These tests control every recovery transition explicitly; production inline
    # maintenance is exercised separately by the restart/lifespan regression.
    await client.app.state.dispatcher.stop_maintenance()
    headers = await login(client)
    original = client.app.state.dispatcher.submit
    async def frozen(*args, **kwargs):
        pass
    client.app.state.dispatcher.submit = frozen
    item = await create(client, headers)
    client.app.state.dispatcher.submit = original
    return headers, item


async def expire(client, run_id):
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, run_id)
        run.lease_expires_at = await database_now(session) - timedelta(seconds=1)
        await session.commit()


async def test_recovery_preserves_checkpoint_and_fences_old_owner(client):
    headers, item = await frozen_run(client)
    old = WorkflowExecutor(client.app)
    assert await old.claim(item["run_id"], 0)
    await old.save_checkpoint(item["run_id"], "budget", {"calls_started": 1}, "fingerprint")
    await expire(client, item["run_id"])
    assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    new = WorkflowExecutor(client.app)
    assert await new.claim(item["run_id"], 1)
    with pytest.raises(LeaseLost):
        await old.emit(item["run_id"], "module_progress", {"phase": "late"})
    await old.fail(item["run_id"], "OLD_FAILURE", "stale failure")
    with pytest.raises(LeaseLost):
        await old.save_checkpoint(item["run_id"], "budget", {"calls_started": 999}, "fingerprint")
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        assert run.status == "running" and run.lease_owner == new.lease.owner
        assert run.job_id.endswith("-e1") and run.recovery_count == 1
        checkpoint = await session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run.id))
        assert checkpoint.payload == {"calls_started": 1}
        assert not await session.scalar(select(Event).where(Event.run_id == run.id, Event.event_type.in_(["run_failed", "run_completed"])))
    await new.fail(item["run_id"], "TEST_CLEANUP", "cleanup")


async def test_heartbeat_renews_only_current_unexpired_lease(client):
    _, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    assert await executor.claim(item["run_id"], 0)
    assert await executor.renew_lease()
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    await expire(client, item["run_id"])
    assert not await executor.renew_lease()
    assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    assert not await executor.renew_lease()


async def test_two_reconcilers_rotate_epoch_only_once(client):
    _, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    await expire(client, item["run_id"])
    results = await asyncio.gather(*[client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0) for _ in range(2)])
    assert results.count(True) == 1
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        assert run.execution_epoch == 1 and run.status == "queued"
        intents = (await session.scalars(select(Outbox).where(Outbox.run_id == run.id, Outbox.event_type == "dispatch_run"))).all()
        assert sum(intent.payload.get("execution_epoch") == 1 for intent in intents) == 1


async def test_expired_attempt_resumes_checkpoint_without_duplicate_versions(client):
    headers, item = await frozen_run(client)
    runtime = client.app.state.runtime
    phases = []
    first = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def generate(data, modules, **options):
        nonlocal attempts
        attempts += 1
        resume = options.get("resume") or {}
        if not resume:
            runtime.assets.consumed.add("runtime-access-history")
            await options["checkpoint_sink"]("module:" + modules[0]["module_id"], {"value": {"module_id": modules[0]["module_id"], "teacher_view_content": "保留模块正文", "canonical_content": {}, "findings": []}, "calls": []})
            first.set()
            await release.wait()
        else:
            phases.append("resumed")
            assert next(iter(resume.values()))["value"]["teacher_view_content"] == "保留模块正文"
        return {"modules": [{"module_id": module["module_id"], "teacher_view_content": "保留模块正文" if index == 0 else "后续模块正文", "canonical_content": {}, "findings": []} for index, module in enumerate(modules)], "calls": []}

    runtime.generate = generate
    old_task = asyncio.create_task(client.app.state.dispatcher._execute(item["run_id"], 0))
    await asyncio.wait_for(first.wait(), 5)
    await expire(client, item["run_id"])
    assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    await client.app.state.dispatcher._execute(item["run_id"], 1)
    release.set()
    await old_task
    assert phases == ["resumed"]
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
    async with client.app.state.db.session_factory() as session:
        versions = (await session.scalars(select(ModuleVersion).where(ModuleVersion.draft_id == item["draft_id"]))).all()
        assert len(versions) == 7
        terminals = (await session.scalars(select(Event).where(Event.run_id == item["run_id"], Event.event_type.in_(["run_completed", "run_failed", "run_cancelled"])))).all()
        assert [event.event_type for event in terminals] == ["run_completed"]


async def test_recovery_limit_closes_run_once_and_cancel_prevents_recovery(client):
    headers, item = await frozen_run(client)
    client.app.state.settings.worker_recovery_limit = 0
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    await expire(client, item["run_id"])
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    run = await wait_run(client, headers, item["run_id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKER_RECOVERY_EXHAUSTED"
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)


async def test_retained_arq_result_rotates_job_id_without_deleting_old_keys(client, monkeypatch):
    from app.tasks import queue

    _, item = await frozen_run(client)
    deliveries = []

    class FakeJob:
        def __init__(self, job_id, *args, **kwargs):
            self.job_id = job_id

        async def status(self):
            return SimpleNamespace(value="complete")

    class FakeRedis:
        async def enqueue_job(self, *args, **kwargs):
            deliveries.append((args, kwargs))
            return SimpleNamespace(job_id=kwargs["_job_id"])

        async def publish(self, *args):
            return 0

        async def aclose(self):
            pass

    monkeypatch.setattr(queue, "Job", FakeJob)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    dispatcher.redis = FakeRedis()
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        # Only an acknowledged publication is reconciled against retained ARQ results.
        intent.status = "published"
        await session.commit()
    await dispatcher.submit(item["run_id"], recover=True)
    assert len(deliveries) == 1
    assert deliveries[0][0] == ("execute_run", item["run_id"], 1)
    assert deliveries[0][1]["_job_id"] == f"run-{item['run_id']}-e1"


async def test_checkpoint_fingerprint_isolation_and_terminal_budget_log(client):
    _, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    payload = {"calls_started": 1, "calls": [{"call_id": "call-1", "call_outcome": "started"}]}
    await executor.save_checkpoint(item["run_id"], "budget", payload, "original")
    with pytest.raises(AppError) as changed:
        await executor.load_checkpoints(item["run_id"], "changed")
    assert changed.value.code == "CHECKPOINT_CONTEXT_CHANGED"
    assert await executor.load_checkpoints(item["run_id"], "original") == {"budget": payload}
    await executor.fail(item["run_id"], "WORKFLOW_TIMEOUT", "timeout")
    await executor.fail(item["run_id"], "SECOND_FAILURE", "ignored")
    async with client.app.state.db.session_factory() as session:
        calls = (await session.scalars(select(CallLog).where(CallLog.run_id == item["run_id"]))).all()
        assert len(calls) == 1 and calls[0].status == "interrupted"
        assert calls[0].metadata_json["call_id"] == "call-1"


async def test_cancelled_run_never_recovers(client):
    headers, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    response = await client.post(f"/api/v1/generation-runs/{item['run_id']}/cancel", headers=headers)
    assert response.status_code == 200
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    with pytest.raises(LeaseLost):
        async with client.app.state.db.session_factory() as session:
            await executor.lock_execution(session, item["run_id"])


async def test_recovery_keeps_original_cumulative_time_budget(client):
    headers, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        moment = await database_now(session)
        run.started_at = moment - timedelta(seconds=301)
        run.lease_expires_at = moment - timedelta(seconds=1)
        await session.commit()
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    run = await wait_run(client, headers, item["run_id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKFLOW_TIMEOUT"
    assert run["execution_epoch"] == 0


async def test_execution_heartbeat_renews_while_provider_is_waiting(client):
    headers, item = await frozen_run(client)
    client.app.state.settings.worker_heartbeat_seconds = 0.1
    client.app.state.settings.worker_lease_seconds = 2
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(data, modules, **options):
        entered.set()
        await release.wait()
        return {"modules": [{"module_id": module["module_id"], "teacher_view_content": "目标清晰的课堂活动",
                             "canonical_content": {}, "findings": []} for module in modules], "calls": []}

    client.app.state.runtime.generate = generate
    task = asyncio.create_task(client.app.state.dispatcher._execute(item["run_id"], 0))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with client.app.state.db.session_factory() as session:
            run = await session.get(Run, item["run_id"])
            previous = run.heartbeat_at
        renewed = False
        for _ in range(50):
            await asyncio.sleep(0.02)
            async with client.app.state.db.session_factory() as session:
                run = await session.get(Run, item["run_id"])
                if run.heartbeat_at > previous:
                    renewed = True
                    break
        assert renewed
        assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
    finally:
        release.set()
        await task
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"


async def test_waiting_for_run_write_lock_cannot_revive_expired_lease(client):
    _, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    await executor.claim(item["run_id"], 0)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        expiry = await database_now(session) + timedelta(seconds=0.25)
        run.lease_expires_at = expiry
        await session.commit()
    async with client.app.state.db.session_factory() as holder:
        await holder.execute(update(Run).where(Run.id == item["run_id"]).values(heartbeat_at=Run.heartbeat_at))
        waiting = asyncio.create_task(executor.renew_lease())
        try:
            await asyncio.sleep(0.1)
            assert not waiting.done()
            await asyncio.sleep(0.25)
        finally:
            await holder.commit()
        assert await waiting is False
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        assert run.lease_expires_at == expiry


async def test_claim_lock_wait_consumes_original_deadline_before_runtime(client):
    headers, item = await frozen_run(client)
    client.app.state.settings.run_timeout_seconds = 0.2
    called = []

    async def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("expired attempt must not enter Runtime")

    client.app.state.runtime.generate = forbidden
    async with client.app.state.db.session_factory() as holder:
        run = await holder.get(Run, item["run_id"])
        run.started_at = await database_now(holder)
        await holder.flush()
        waiting = asyncio.create_task(client.app.state.dispatcher._execute(item["run_id"], 0))
        try:
            await asyncio.sleep(0.35)
        finally:
            await holder.commit()
        await waiting
    assert called == []
    run = await wait_run(client, headers, item["run_id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKFLOW_TIMEOUT"


@pytest.mark.parametrize("kind", ["polish", "review"])
async def test_polish_and_review_restore_candidate_and_budget_once(client, kind):
    headers, item = await frozen_run(client)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        draft = await session.get(Draft, run.draft_id)
        if kind == "polish":
            polish = PolishSession(id=uid(), user_id=run.user_id, draft_id=draft.id,
                module_id=draft.modules[0]["module_id"], scope="module")
            session.add(polish)
            await session.flush()
            message = PolishMessage(id=uid(), session_id=polish.id, run_id=run.id,
                instruction="保持目标明确", working_content="学生完成折纸活动")
            session.add(message)
            run.workflow_type = "module_polish_message"
            run.payload = {"session_id": polish.id, "message_id": message.id, "module_id": polish.module_id,
                "scope": "module", "working_content": message.working_content, "instruction": message.instruction}
            polish_id = polish.id
        else:
            run.workflow_type = "lesson_internal_review"
            run.payload = {}
        await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    provider_calls = []
    ledger = [{"call_id": "call-1", "component_name": kind, "call_outcome": "success"}]
    value = {"content": "恢复后已通过校验的候选", "findings": []} if kind == "polish" else {"findings": [{"code": "reviewed", "severity": "info"}]}

    async def runtime(*args, **options):
        resume = options.get("resume") or {}
        if kind not in resume:
            provider_calls.append(kind)
            await options["checkpoint_sink"](kind, {"value": value, "calls": ledger,
                "calls_started": 1, "attempt_counts": {kind: 1}})
            entered.set()
            await release.wait()
        else:
            assert resume[kind]["calls_started"] == 1
            assert resume[kind]["value"] == value
        return {**value, "calls": ledger}

    setattr(client.app.state.runtime, kind, runtime)
    old = asyncio.create_task(client.app.state.dispatcher._execute(item["run_id"], 0))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await expire(client, item["run_id"])
        assert await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
        await client.app.state.dispatcher._execute(item["run_id"], 1)
    finally:
        release.set()
        await old
    assert provider_calls == [kind]
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
    async with client.app.state.db.session_factory() as session:
        calls = (await session.scalars(select(CallLog).where(CallLog.run_id == item["run_id"]))).all()
        assert len(calls) == 1
        if kind == "polish":
            assert (await session.get(PolishSession, polish_id)).candidate == value["content"]
        else:
            checks = (await session.scalars(select(QualityResult).where(QualityResult.run_id == item["run_id"]))).all()
            assert len(checks) == 1 and checks[0].findings == value["findings"]


@pytest.mark.parametrize("legacy_lock", [False, True], ids=["current-lock", "legacy-fk-recheck-control"])
async def test_postgres_heartbeat_and_event_writer_do_not_deadlock(monkeypatch, legacy_lock):
    """Opt-in only: caller creates and later drops its unique empty verification database."""
    import os
    from types import SimpleNamespace

    from sqlalchemy import func, text
    from sqlalchemy.engine import make_url

    from app.core.config import Settings
    from app.db.models import User
    from app.db.session import Database
    from app.services import lessons

    source = os.environ.get("LESSON_LOCK_TEST_DATABASE_URL")
    if not source:
        pytest.skip("Explicit dedicated PostgreSQL lock-verification database is required")
    url = make_url(source)
    if (url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1" or url.query
        or not url.database or not url.database.startswith("lesson_deadlock_verify_")):
        raise ValueError("Lock verification requires a dedicated loopback lesson_deadlock_verify_ database")
    database = Database(Settings(_env_file=None, environment="test", database_url=source,
        ai_provider="mock", storage_backend="local"))
    settings = Settings(_env_file=None, environment="test", ai_provider="mock", storage_backend="local")
    executor = WorkflowExecutor(SimpleNamespace(state=SimpleNamespace(db=database, settings=settings)))
    owner = uid()
    async with database.session_factory() as session:
        user = User(id=uid(), email=f"lock-{uid()}@example.test", password_hash="unused")
        session.add(user)
        await session.flush()
        draft = Draft(id=uid(), user_id=user.id, tenant_id=user.tenant_id,
            request_id=uid(), input_hash="lock-verification", topic="合成锁验证")
        session.add(draft)
        await session.flush()
        run = Run(id=uid(), user_id=user.id, tenant_id=user.tenant_id, draft_id=draft.id,
            workflow_type="one_click", idempotency_key=uid(), input_hash="lock-verification",
            status="running", execution_epoch=0, lease_owner=owner,
            lease_expires_at=await database_now(session) + timedelta(seconds=60))
        session.add(run)
        await session.commit()
        run_id, draft_id = run.id, draft.id
    executor.lease = lessons.ExecutionLease(run_id, 0, owner)
    draft_locked, heartbeat_locked = asyncio.Event(), asyncio.Event()
    allow_generation, allow_heartbeat = asyncio.Event(), asyncio.Event()
    pids = {}
    original_clock = lessons.database_now
    original_lock = lessons.lock_matching_run

    async def legacy_heartbeat_lock(session, *conditions):
        if legacy_lock and asyncio.current_task().get_name() == "verify-heartbeat":
            changed = await session.execute(update(Run).where(*conditions).values(heartbeat_at=Run.heartbeat_at)
                .execution_options(synchronize_session=False))
            return bool(changed.rowcount)
        return await original_lock(session, *conditions)

    monkeypatch.setattr(lessons, "lock_matching_run", legacy_heartbeat_lock)

    async def paused_clock(session):
        if asyncio.current_task().get_name() == "verify-heartbeat":
            pids["heartbeat"] = await session.scalar(select(func.pg_backend_pid()))
            heartbeat_locked.set()
            await allow_heartbeat.wait()
        return await original_clock(session)

    monkeypatch.setattr(lessons, "database_now", paused_clock)

    async def generation():
        async with database.session_factory() as session:
            pids["generation"] = await session.scalar(select(func.pg_backend_pid()))
            await session.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
            draft_locked.set()
            await allow_generation.wait()
            current, _ = await executor.lock_execution(session, run_id)
            await lessons.event(session, current, "module_progress", {"phase": "lock_verification"})
            await session.commit()

    writer = asyncio.create_task(generation(), name="verify-generation")
    heartbeat = None
    try:
        await asyncio.wait_for(draft_locked.wait(), 5)
        heartbeat = asyncio.create_task(executor.renew_lease(), name="verify-heartbeat")
        await asyncio.wait_for(heartbeat_locked.wait(), 5)
        allow_generation.set()
        blocked = False
        async with asyncio.timeout(5):
            while not blocked:
                async with database.session_factory() as probe:
                    blockers = await probe.scalar(select(func.pg_blocking_pids(pids["generation"])))
                blocked = pids["heartbeat"] in blockers
                if not blocked:
                    await asyncio.sleep(0.01)
        allow_heartbeat.set()
        parent_fk_lock = False
        # The legacy no-op creates a current-transaction row version; the second
        # UPDATE then rechecks its Draft FK and requests a parent KEY SHARE lock.
        for _ in range(30):
            async with database.session_factory() as probe:
                parent_fk_lock = bool(await probe.scalar(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_locks
                    WHERE pid = :pid AND relation = 'ai_lesson_drafts'::regclass
                      AND mode = 'RowShareLock')
                """), {"pid": pids["heartbeat"]}))
            if parent_fk_lock or heartbeat.done():
                break
            await asyncio.sleep(0.01)
        results = await asyncio.wait_for(asyncio.gather(writer, heartbeat, return_exceptions=True), 5)
        codes = [str(getattr(getattr(result, "orig", result), "sqlstate", type(result).__name__))
                 for result in results if isinstance(result, BaseException)]
        if legacy_lock:
            assert "40P01" in codes, "Legacy control must reproduce the PostgreSQL deadlock"
            assert parent_fk_lock, "Legacy heartbeat must prove the parent Draft FK lock inversion"
        else:
            assert not codes, "Heartbeat/event lock cycle: " + ", ".join(codes)
            assert not parent_fk_lock
            assert results[1] is True
    finally:
        allow_generation.set()
        allow_heartbeat.set()
        for task in (writer, heartbeat):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in (writer, heartbeat) if task], return_exceptions=True)
        await database.engine.dispose()
