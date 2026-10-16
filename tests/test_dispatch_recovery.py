import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import event as sql_event
from sqlalchemy import select, update

from app.db.models import Event, Outbox, Run
from app.services.lessons import database_now
from tests.test_execution_recovery import frozen_run


class Queue:
    def __init__(self, client, *, fail=True):
        self.client, self.fail, self.deliveries = client, fail, []

    async def enqueue_job(self, *args, **kwargs):
        async with self.client.app.state.db.session_factory() as session:
            run = await session.get(Run, args[1])
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == run.id,
                Outbox.event_type == "dispatch_run", Outbox.status == "dispatching"))
            assert intent is not None and intent.attempts > 0
            assert intent.last_attempt_at and intent.dispatch_deadline_at
        self.deliveries.append((args, kwargs))
        if self.fail:
            raise RuntimeError("redis://secret:do-not-log@private.example/0 教师正文")
        return SimpleNamespace(job_id=kwargs["_job_id"])

    async def publish(self, *args):
        return 0

    async def aclose(self):
        pass


async def due(client, run_id):
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == run_id,
            Outbox.event_type == "dispatch_run").order_by(Outbox.created_at.desc()))
        intent.available_at = await database_now(session) - timedelta(seconds=1)
        await session.commit()


async def test_first_failure_persists_budget_before_io_then_recovers_and_resolves_alert(client, caplog):
    from app.db.models import RecoveryAlert

    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    queue = Queue(client)
    client.app.state.settings.task_queue_mode = "arq"
    dispatcher.redis = queue
    await dispatcher.submit(item["run_id"])
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == run.id))
        assert run.status == "queued"
        assert intent.status == "pending" and intent.attempts == 1
        assert intent.last_error_code == "DISPATCH_UNAVAILABLE"
        assert intent.available_at > intent.last_attempt_at
        assert intent.last_error_at and intent.dispatch_deadline_at
        deadline = intent.dispatch_deadline_at
        alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == run.id))
        assert alert.code == "DISPATCH_RETRY" and alert.status == "open"
    await dispatcher.submit(item["run_id"], recover=True)
    assert len(queue.deliveries) == 1  # Backoff cannot be bypassed by normal reconciliation.
    queue.fail = False
    await due(client, item["run_id"])
    await dispatcher.submit(item["run_id"], recover=True)
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        assert intent.status == "published" and intent.attempts == 2
        assert intent.dispatch_deadline_at == deadline
        alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == item["run_id"]))
        assert alert.status == "resolved" and alert.resolved_at
    assert "do-not-log" not in caplog.text and "教师正文" not in caplog.text


async def test_dispatch_attempts_exhaust_once_without_reset_or_terminal_revival(client):
    from app.db.models import RecoveryAlert

    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    queue = Queue(client)
    client.app.state.settings.task_queue_mode = "arq"
    client.app.state.settings.dispatch_max_attempts = 2
    dispatcher.redis = queue
    await dispatcher.submit(item["run_id"])
    await due(client, item["run_id"])
    await dispatcher.submit(item["run_id"], recover=True)
    await dispatcher.submit(item["run_id"], recover=True)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        assert run.status == "failed" and run.error_code == "DISPATCH_EXHAUSTED"
        terminal = (await session.scalars(select(Event).where(Event.run_id == run.id, Event.event_type == "run_failed"))).all()
        assert len(terminal) == 1
        alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == run.id, RecoveryAlert.code == "DISPATCH_EXHAUSTED"))
        assert alert.status == "open"
    assert len(queue.deliveries) == 2


async def test_alert_dedupe_ack_recurrence_and_resolved_reopen(client):
    from app.db.models import RecoveryAlert
    from app.services.recovery import resolve_alerts, signal_alert

    _, item = await frozen_run(client)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        alert = await signal_alert(session, "DISPATCH_RETRY", "受控消息", run=run)
        await session.commit()
        alert.status = "acknowledged"
        alert.acknowledged_by = run.user_id
        alert.acknowledged_at = await database_now(session)
        await session.commit()
        await signal_alert(session, "DISPATCH_RETRY", "受控消息", run=run, metadata={"attempts": 2, "secret": "never-store"})
        await session.commit()
        await session.refresh(alert)
        assert alert.status == "acknowledged" and alert.occurrence_count == 2
        assert alert.metadata_json == {"attempts": 2}
        await resolve_alerts(session, run.id, ["DISPATCH_RETRY"])
        await session.commit()
        await signal_alert(session, "DISPATCH_RETRY", "受控消息", run=run)
        await session.commit()
        await session.refresh(alert)
        assert alert.status == "open" and alert.occurrence_count == 3
        assert alert.acknowledged_by is None and alert.resolved_at is None
        assert len((await session.scalars(select(RecoveryAlert).where(RecoveryAlert.run_id == run.id))).all()) == 1


async def test_concurrent_dispatch_and_late_receipt_cannot_overwrite_cancellation(client):
    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingQueue(Queue):
        async def enqueue_job(self, *args, **kwargs):
            receipt = await super().enqueue_job(*args, **kwargs)
            entered.set()
            await release.wait()
            return receipt

    queue = BlockingQueue(client, fail=False)
    dispatcher.redis = queue
    task = asyncio.create_task(dispatcher.submit(item["run_id"]))
    await asyncio.wait_for(entered.wait(), 2)
    await dispatcher.submit(item["run_id"])
    assert len(queue.deliveries) == 1
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        run.status, run.cancel_requested = "cancelled", True
        await session.commit()
    release.set()
    await task
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == run.id))
        assert run.status == "cancelled" and intent.status == "cancelled"
        assert intent.attempts == 1
        assert not await session.scalar(select(Event).where(Event.run_id == run.id,
            Event.payload["phase"].as_string() == "dispatch_published"))


async def test_unknown_receipt_and_interrupted_claim_preserve_stable_job_and_budget(client, monkeypatch):
    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    queue = Queue(client, fail=False)
    dispatcher.redis = queue
    claim = await dispatcher._reserve(item["run_id"])
    assert claim["attempts"] == 1
    assert await dispatcher._reserve(item["run_id"]) is None
    await due(client, item["run_id"])

    class ForeignJob:
        def __init__(self, *args, **kwargs):
            pass
        async def info(self):
            return SimpleNamespace(args=("another-run", 0), function="execute_run")

    async def unknown(*args, **kwargs):
        await Queue.enqueue_job(queue, *args, **kwargs)
        return None

    monkeypatch.setattr("app.tasks.queue.Job", ForeignJob)
    monkeypatch.setattr(queue, "enqueue_job", unknown)
    await dispatcher.submit(item["run_id"], recover=True)
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        assert intent.status == "pending" and intent.attempts == 2
        assert intent.last_error_code == "DISPATCH_RECEIPT_UNKNOWN"
    assert queue.deliveries[0][1]["_job_id"] == claim["job_id"]


async def test_dispatch_deadline_exhausts_without_external_io_and_recovery_keeps_budget(client):
    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    queue = Queue(client, fail=False)
    dispatcher.redis = queue
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == run.id))
        moment = await database_now(session)
        run.status, run.lease_expires_at = "running", moment - timedelta(seconds=1)
        intent.attempts, intent.dispatch_deadline_at = 3, moment - timedelta(seconds=1)
        old_deadline = intent.dispatch_deadline_at
        await session.commit()
    assert await dispatcher.recover_run(item["run_id"], expected_epoch=0)
    await dispatcher.submit(item["run_id"], recover=True)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == run.id,
            Outbox.payload["execution_epoch"].as_integer() == 1))
        assert run.status == "failed" and run.error_code == "DISPATCH_EXHAUSTED"
        assert intent.attempts == 3 and intent.dispatch_deadline_at == old_deadline
        assert intent.status == "exhausted"
    assert queue.deliveries == []


async def test_due_scan_is_not_starved_by_one_hundred_future_published_intents(client):
    from app.services.lessons import uid

    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    queue = Queue(client, fail=False)
    dispatcher.redis = queue
    async with client.app.state.db.session_factory() as session:
        original = await session.get(Run, item["run_id"])
        moment = await database_now(session)
        for index in range(101):
            run = Run(id=uid(), user_id=original.user_id, tenant_id=original.tenant_id,
                workflow_type="export", idempotency_key=f"future-{index}", input_hash="hash")
            session.add(run)
            await session.flush()
            session.add(Outbox(id=uid(), run_id=run.id, event_type="dispatch_run", status="published",
                payload={"run_id": run.id}, available_at=moment + timedelta(seconds=100)))
        await session.commit()
    await dispatcher.reconcile()
    assert len(queue.deliveries) == 1 and queue.deliveries[0][0][1] == item["run_id"]


async def test_alert_scan_is_scalar_bounded_and_advances_past_first_page(client, monkeypatch):
    from app.db.models import RecoveryAlert
    from app.services import recovery
    from app.services.lessons import uid

    _, item = await frozen_run(client)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        moment = await database_now(session)
        run.created_at = moment - timedelta(seconds=120)
        for index in range(3):
            session.add(Run(id=uid(), user_id=run.user_id, tenant_id=run.tenant_id,
                workflow_type="export", idempotency_key=f"backlog-{index}", input_hash="hash",
                created_at=run.created_at, payload={"sensitive_body": "not-selected"}))
        await session.commit()
    monkeypatch.setattr(recovery, "ALERT_SCAN_BATCH", 2, raising=False)
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    engine = client.app.state.db.engine.sync_engine
    sql_event.listen(engine, "before_cursor_execute", capture)
    try:
        await recovery.scan_alerts(client.app)
        async with client.app.state.db.session_factory() as session:
            alerts = (await session.scalars(select(RecoveryAlert))).all()
            assert len(alerts) == 2
        await recovery.scan_alerts(client.app)
    finally:
        sql_event.remove(engine, "before_cursor_execute", capture)
    assert not any("ai_generation_runs.payload" in statement for statement in statements)
    async with client.app.state.db.session_factory() as session:
        alerts = (await session.scalars(select(RecoveryAlert))).all()
        assert len(alerts) == 4
        await session.execute(update(Run).values(status="completed"))
        await session.commit()
    await recovery.scan_alerts(client.app)
    await recovery.scan_alerts(client.app)
    async with client.app.state.db.session_factory() as session:
        alerts = (await session.scalars(select(RecoveryAlert))).all()
        assert all(alert.status == "resolved" and alert.resolved_at for alert in alerts)


async def test_queue_connect_suppresses_upstream_raw_exception_logging(client, monkeypatch, caplog):
    from redis.exceptions import ConnectionError

    class UnreachableRedis:
        def __init__(self, *args, **kwargs):
            pass
        async def ping(self):
            raise ConnectionError("private-host API_SECRET teacher-body")

    monkeypatch.setattr("arq.connections.ArqRedis", UnreachableRedis)
    client.app.state.settings.task_queue_mode = "arq"
    client.app.state.settings.dispatch_timeout_seconds = 0.05
    await client.app.state.dispatcher.connect()
    assert client.app.state.dispatcher.redis is None
    assert "API_SECRET" not in caplog.text and "teacher-body" not in caplog.text


async def test_network_timeout_remains_retryable_with_persisted_attempt(client):
    _, item = await frozen_run(client)
    dispatcher = client.app.state.dispatcher
    client.app.state.settings.task_queue_mode = "arq"
    client.app.state.settings.dispatch_timeout_seconds = 0.01

    class SlowQueue(Queue):
        async def enqueue_job(self, *args, **kwargs):
            await super().enqueue_job(*args, **kwargs)
            await asyncio.sleep(10)

    dispatcher.redis = SlowQueue(client, fail=False)
    await dispatcher.submit(item["run_id"])
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        run = await session.get(Run, item["run_id"])
        assert run.status == "queued" and intent.status == "pending"
        assert intent.attempts == 1 and intent.last_error_code == "DISPATCH_TIMEOUT"


def test_retry_jitter_is_bounded_and_capped():
    from app.tasks.queue import Dispatcher

    settings = SimpleNamespace(dispatch_retry_base_seconds=2, dispatch_retry_max_seconds=60)
    dispatcher = Dispatcher(SimpleNamespace(state=SimpleNamespace(settings=settings)))
    for attempts, ceiling in [(1, 2), (2, 4), (6, 60), (10**6, 60)]:
        for _ in range(50):
            assert 0.8 * ceiling <= dispatcher.retry_delay(attempts) <= ceiling


async def test_inline_restart_waits_for_durable_claim_expiry_then_automatically_resumes(client):
    from app.tasks.queue import Dispatcher

    _, item = await frozen_run(client)
    old = client.app.state.dispatcher
    await old.close()
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        moment = await database_now(session)
        intent.status, intent.attempts = "dispatching", 1
        intent.available_at = moment + timedelta(seconds=0.3)
        intent.dispatch_deadline_at = moment + timedelta(seconds=300)
        await session.commit()
    restarted = Dispatcher(client.app)
    restarted.maintenance_interval_seconds = 0.02
    client.app.state.dispatcher = restarted
    await restarted.connect()
    await restarted.reconcile()  # The lifespan's one startup pass must honor the existing claim.
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == item["run_id"]))
        assert intent.status == "dispatching" and intent.attempts == 1
    try:
        status = "queued"
        for _ in range(40):
            await asyncio.sleep(0.05)
            async with client.app.state.db.session_factory() as session:
                status = (await session.get(Run, item["run_id"])).status
            if status in {"running", "completed", "failed"}:
                break
        assert status in {"running", "completed"}, "Inline restart left a future dispatch reservation permanently queued"
    finally:
        await restarted.close()
    async with client.app.state.db.session_factory() as session:
        assert (await session.get(Run, item["run_id"])).status == "completed"


async def test_arq_domain_event_without_connection_remains_pending_then_publishes(client):
    from app.services.lessons import uid

    dispatcher = client.app.state.dispatcher
    await dispatcher.stop_maintenance()
    client.app.state.settings.task_queue_mode = "arq"
    intent_id = uid()
    async with client.app.state.db.session_factory() as session:
        session.add(Outbox(id=intent_id, event_type="module_committed", payload={"draft_id": "test"}))
        await session.commit()
    await dispatcher.reconcile()
    async with client.app.state.db.session_factory() as session:
        intent = await session.get(Outbox, intent_id)
        assert intent.status == "pending" and intent.attempts == 1
        assert intent.last_error_code == "OUTBOX_UNAVAILABLE" and intent.last_error_at
        assert intent.available_at > await database_now(session)
        intent.available_at = await database_now(session) - timedelta(seconds=1)
        await session.commit()

    sent = []
    class Redis:
        async def xadd(self, *args, **kwargs):
            sent.append(args)
            return "123-0"
        async def aclose(self):
            pass
    dispatcher.redis = Redis()
    await dispatcher.reconcile()
    assert len(sent) == 1 and sent[0][0] == "lesson:domain-events"
    async with client.app.state.db.session_factory() as session:
        intent = await session.get(Outbox, intent_id)
        assert intent.status == "published" and intent.attempts == 2
