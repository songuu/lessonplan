"""Durable dispatch and lease recovery; PostgreSQL owns execution, ARQ owns delivery."""

import asyncio
import json
import random
from datetime import timedelta

from arq import create_pool
from arq.connections import RedisSettings
from arq.jobs import Job
from sqlalchemy import or_, select, update

from app.db.models import Issue, Outbox, Run
from app.services.lessons import (
    LeaseLost,
    WorkflowExecutor,
    database_now,
    event,
    lock_matching_run,
    lock_run_with_draft,
    uid,
)
from app.services.recovery import recovery_log, resolve_alerts, scan_alerts, signal_alert

QUEUES = {"one-click": 8, "structured": 3, "module-polish": 6, "export": 4,
          "thumbnail": 4, "internal-review": 2, "maintenance": 2}


def route_for(workflow):
    suffix = {"one_click": "one-click", "interactive_step": "structured",
        "module_polish_message": "module-polish", "reflection_polish_message": "module-polish",
        "export": "export", "document_parse": "thumbnail", "knowledge_document_parse": "thumbnail",
        "lesson_internal_review": "internal-review"}.get(workflow, "maintenance")
    return f"lesson:{suffix}"


class Dispatcher:
    maintenance_interval_seconds = 2

    def __init__(self, app):
        self.app, self.redis = app, None
        self.tasks, self.lock = set(), asyncio.Lock()
        self._notify_after = 0
        self._maintenance_task = None

    async def connect(self):
        if self.app.state.settings.task_queue_mode == "inline":
            if self._maintenance_task is None or self._maintenance_task.done():
                self._maintenance_task = asyncio.create_task(self._maintain_inline(), name="inline-maintenance")
            return
        if self.app.state.settings.task_queue_mode == "arq":
            try:
                async with asyncio.timeout(self.app.state.settings.dispatch_timeout_seconds):
                    settings = RedisSettings.from_dsn(self.app.state.settings.redis_url)
                    # Our persisted dispatcher owns retries; ARQ's internal loop logs raw errors.
                    settings.conn_retries = 0
                    self.redis = await create_pool(settings)
            except Exception:
                recovery_log("queue_connection_unavailable", code="DISPATCH_UNAVAILABLE")

    async def _maintain_inline(self):
        # A fast process restart may find an unexpired dispatch claim. A single startup
        # pass correctly skips it, so inline mode needs the same due scans as ARQ cron.
        while self.app.state.settings.task_queue_mode == "inline":
            await asyncio.sleep(self.maintenance_interval_seconds)
            if self.app.state.settings.task_queue_mode != "inline":
                return
            try:
                await self.reconcile()
            except Exception:
                recovery_log("recovery_maintenance_unavailable", code="MAINTENANCE_UNAVAILABLE")

    async def stop_maintenance(self):
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None

    async def notify(self, run_id):
        moment = asyncio.get_running_loop().time()
        if self.redis and moment >= self._notify_after:
            try:
                async with asyncio.timeout(min(self.app.state.settings.dispatch_timeout_seconds, 0.25)):
                    await self.redis.publish(f"lesson:run:{run_id}", "changed")
            except Exception:
                self._notify_after = moment + 5
                recovery_log("queue_notification_unavailable", code="DISPATCH_UNAVAILABLE", run_id=run_id)

    def retry_delay(self, attempts):
        settings = self.app.state.settings
        maximum = min(settings.dispatch_retry_max_seconds,
            settings.dispatch_retry_base_seconds * 2 ** min(max(attempts - 1, 0), 30))
        return random.uniform(maximum * 0.8, maximum)

    async def submit(self, run_id, *, recover=False):
        claim = await self._reserve(run_id)
        if claim is None:
            return
        if claim["action"] == "exhaust":
            await self._exhaust(claim)
            return
        try:
            async with asyncio.timeout(claim["timeout"]):
                outcome = await self._deliver(claim)
            code = None
        except TimeoutError:
            outcome, code = "error", "DISPATCH_TIMEOUT"
        except Exception:
            outcome, code = "error", "DISPATCH_UNAVAILABLE"
        # Cancellation/crash deliberately leaves a durable in-flight reservation.
        followup = await self._complete(claim, outcome, code)
        if followup == "exhaust":
            await self._exhaust(claim)
        elif followup == "dispatch":
            await self.submit(run_id, recover=True)
        elif followup == "rotate":
            if await self.recover_run(run_id, expected_epoch=claim["epoch"], queued_job_lost=True):
                await self.submit(run_id, recover=True)

    async def _reserve(self, run_id):
        async with self.app.state.db.session_factory() as session:
            run, _ = await lock_run_with_draft(session, run_id)
            if not run:
                return None
            await lock_matching_run(session, Run.id == run_id)
            await session.refresh(run)
            moment = await database_now(session)
            intents = (await session.scalars(select(Outbox).where(Outbox.run_id == run_id,
                Outbox.event_type == "dispatch_run").order_by(Outbox.created_at.desc()))).all()
            if run.status != "queued" or run.cancel_requested:
                for item in intents:
                    if item.status not in {"exhausted", "superseded", "cancelled"}:
                        item.status = "published" if run.status == "running" and not run.cancel_requested else "cancelled"
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY", "QUEUE_BACKLOG"]
                    + (["WORKER_LEASE_EXPIRED"] if run.cancel_requested or run.status != "running" else []))
                await session.commit()
                return None
            for old in intents:
                if old.payload.get("execution_epoch", 0) != run.execution_epoch and old.status not in {"cancelled", "exhausted"}:
                    old.status = "superseded"
            intent = next((item for item in intents if item.payload.get("execution_epoch", 0) == run.execution_epoch), None)
            if intent is None:
                intent = Outbox(id=uid(), run_id=run_id, event_type="dispatch_run", status="pending",
                    payload={"run_id": run_id, "execution_epoch": run.execution_epoch}, attempts=max((x.attempts for x in intents), default=0),
                    created_at=moment, available_at=moment)
                session.add(intent)
            if intent.dispatch_deadline_at is None:
                origin = min((x.created_at for x in intents), default=intent.created_at or moment)
                intent.dispatch_deadline_at = origin + timedelta(seconds=self.app.state.settings.dispatch_deadline_seconds)
            if intent.status in {"exhausted", "superseded", "cancelled"}:
                await session.commit()
                return None
            if intent.available_at and intent.available_at > moment:
                await session.commit()
                return None
            if moment >= intent.dispatch_deadline_at:
                action = "exhaust"
            elif intent.status in {"published", "checking"}:
                action = "check"
            elif intent.attempts >= self.app.state.settings.dispatch_max_attempts:
                action = "exhaust"
            else:
                action = "enqueue"
            if action == "enqueue":
                intent.attempts += 1
                intent.last_attempt_at = moment
            intent.status = "dispatching" if action == "enqueue" else "checking"
            intent.available_at = moment + timedelta(seconds=self.app.state.settings.dispatch_timeout_seconds + 1)
            await session.flush()
            claim = {"run_id": run.id, "intent_id": intent.id, "epoch": run.execution_epoch,
                "attempts": intent.attempts, "until": intent.available_at, "job_id": run.job_id,
                "queue_name": run.queue_name, "action": action,
                "timeout": max(0.001, min(self.app.state.settings.dispatch_timeout_seconds,
                    (intent.dispatch_deadline_at - moment).total_seconds()))}
            # The attempt is durable before even the first network/inline delivery operation.
            await session.commit()
            return claim

    async def _deliver(self, claim):
        run_id, epoch = claim["run_id"], claim["epoch"]
        if self.app.state.settings.task_queue_mode == "inline":
            name = f"run-{run_id}-e{epoch}"
            existing = any(task.get_name() == name and not task.done() for task in self.tasks)
            if claim["action"] == "check":
                return "published" if existing else "missing"
            if not existing:
                task = asyncio.create_task(self._execute(run_id, epoch), name=name)
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            return "published"
        if self.redis is None:
            await self.connect()
        if self.redis is None:
            raise RuntimeError("queue unavailable")
        job = Job(claim["job_id"], self.redis, _queue_name=claim["queue_name"])
        if claim["action"] == "check":
            state = await job.status()
            if state.value in {"queued", "in_progress", "deferred"}:
                return "published"
            if state.value in {"complete", "failed"}:
                return "rotate"
            return "missing"
        receipt = await self.redis.enqueue_job("execute_run", run_id, epoch,
            _job_id=claim["job_id"], _queue_name=claim["queue_name"])
        if receipt is None:
            info = await job.info()
            expected = {(run_id, epoch)}
            if epoch == 0:
                expected.add((run_id,))
            if not info or not isinstance(info.args, tuple) or info.args not in expected or getattr(info, "function", "execute_run") != "execute_run":
                return "unknown"
        return "published"

    async def _complete(self, claim, outcome, code):
        followup = None
        async with self.app.state.db.session_factory() as session:
            run, _ = await lock_run_with_draft(session, claim["run_id"])
            if not run:
                return None
            await lock_matching_run(session, Run.id == run.id)
            await session.refresh(run)
            intent = await session.get(Outbox, claim["intent_id"])
            moment = await database_now(session)
            if (not intent or intent.attempts != claim["attempts"] or intent.available_at != claim["until"]
                or intent.status not in {"dispatching", "checking"}):
                return None
            if run.execution_epoch != claim["epoch"]:
                intent.status = "superseded"
            elif run.status not in {"queued", "running"} or run.cancel_requested:
                intent.status = "cancelled"
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY", "QUEUE_BACKLOG", "WORKER_LEASE_EXPIRED"])
            elif moment >= claim["until"]:
                return None
            elif run.status == "running":
                intent.status = "published"
                intent.available_at = moment + timedelta(seconds=5)
                intent.last_error_code = None
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY", "QUEUE_BACKLOG", "WORKER_LEASE_EXPIRED"])
            elif moment >= intent.dispatch_deadline_at:
                followup = "exhaust"
            elif outcome == "published":
                intent.status, intent.last_error_code = "published", None
                intent.available_at = moment + timedelta(seconds=5)
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY"])
                await event(session, run, "module_progress", {"phase": "dispatch_published", "attempt": intent.attempts,
                    "execution_epoch": run.execution_epoch})
            elif outcome in {"missing", "rotate"}:
                intent.status, intent.available_at = "pending", moment
                followup = "dispatch" if outcome == "missing" else "rotate"
            else:
                code = code or "DISPATCH_RECEIPT_UNKNOWN"
                intent.status = "published" if claim["action"] == "check" else "pending"
                intent.last_error_code, intent.last_error_at = code, moment
                intent.available_at = min(intent.dispatch_deadline_at,
                    moment + timedelta(seconds=self.retry_delay(intent.attempts)))
                await signal_alert(session, "DISPATCH_RETRY", "", run,
                    metadata={"attempts": intent.attempts, "max_attempts": self.app.state.settings.dispatch_max_attempts,
                              "execution_epoch": run.execution_epoch, "last_error_code": code})
                await event(session, run, "module_progress", {"phase": "dispatch_retry_scheduled",
                    "attempt": intent.attempts, "max_attempts": self.app.state.settings.dispatch_max_attempts,
                    "execution_epoch": run.execution_epoch, "code": code})
                recovery_log("dispatch_retry_scheduled", code=code, run_id=run.id, status="pending",
                    metadata={"attempts": intent.attempts, "execution_epoch": run.execution_epoch})
                if claim["action"] == "enqueue" and intent.attempts >= self.app.state.settings.dispatch_max_attempts:
                    followup = "exhaust"
            await session.commit()
        await self.notify(claim["run_id"])
        return followup

    async def _exhaust(self, claim):
        await WorkflowExecutor(self.app).fail(claim["run_id"], "DISPATCH_EXHAUSTED",
            "任务投递达到尝试次数或排队期限上限，请重新生成", expected_epoch=claim["epoch"])
        async with self.app.state.db.session_factory() as session:
            run, _ = await lock_run_with_draft(session, claim["run_id"])
            if not run or run.execution_epoch != claim["epoch"]:
                return
            await lock_matching_run(session, Run.id == run.id)
            await session.refresh(run)
            intent = await session.get(Outbox, claim["intent_id"])
            if not intent:
                return
            if run.status == "failed" and run.error_code == "DISPATCH_EXHAUSTED":
                intent.status = "exhausted"
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY", "QUEUE_BACKLOG"])
                await signal_alert(session, "DISPATCH_EXHAUSTED", "", run, severity="error",
                    metadata={"attempts": intent.attempts, "execution_epoch": run.execution_epoch})
            elif run.status == "running":
                intent.status = "published"
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY"])
            elif run.status in {"cancelled", "completed", "failed"}:
                intent.status = "cancelled"
            await session.commit()

    async def _execute(self, run_id, expected_epoch=0):
        executor = WorkflowExecutor(self.app)
        try:
            async with self.app.state.db.session_factory() as session:
                run = await session.get(Run, run_id)
                if not run or run.execution_epoch != expected_epoch or run.status != "queued":
                    return
                elapsed = max(0, ((await database_now(session)) - run.started_at).total_seconds()) if run.started_at else 0
            remaining = self.app.state.settings.run_timeout_seconds - elapsed
            if remaining <= 0:
                await executor.fail(run_id, "WORKFLOW_TIMEOUT", "生成超过累计时间预算，请重试", expected_epoch=expected_epoch)
                return
            # A deadline must not split COMMIT and publishing its owner token in this process.
            if not await executor.claim(run_id, expected_epoch):
                return
            async with self.app.state.db.session_factory() as session:
                run = await session.get(Run, run_id)
                elapsed = max(0, ((await database_now(session)) - run.started_at).total_seconds())
            remaining = self.app.state.settings.run_timeout_seconds - elapsed
            if remaining <= 0:
                await executor.fail(run_id, "WORKFLOW_TIMEOUT", "生成超过累计时间预算，请重试")
                return
            async with asyncio.timeout(remaining):
                await executor.execute(run_id, expected_epoch)
        except LeaseLost:
            return
        except TimeoutError:
            await executor.fail(run_id, "WORKFLOW_TIMEOUT", "生成超过时间预算，请重试")
        except asyncio.CancelledError:
            if executor.lease_lost:
                return
            await executor.abandon()
            raise

    async def recover_run(self, run_id, *, expected_epoch=None, queued_job_lost=False):
        exhausted_epoch = None
        exhausted_code = "WORKER_RECOVERY_EXHAUSTED"
        async with self.app.state.db.session_factory() as session:
            run, _ = await lock_run_with_draft(session, run_id)
            if not run or run.cancel_requested or run.status not in {"queued", "running"}:
                return False
            epoch = run.execution_epoch
            if expected_epoch is not None and epoch != expected_epoch:
                return False
            moment = await database_now(session)
            expired = run.status == "running" and (run.lease_expires_at is None or run.lease_expires_at <= moment)
            if not expired and not (queued_job_lost and run.status == "queued"):
                return False
            over_deadline = run.started_at and (moment - run.started_at).total_seconds() >= self.app.state.settings.run_timeout_seconds
            if over_deadline or run.recovery_count >= self.app.state.settings.worker_recovery_limit:
                exhausted_epoch = epoch
                exhausted_code = "WORKFLOW_TIMEOUT" if over_deadline else "WORKER_RECOVERY_EXHAUSTED"
            else:
                conditions = [Run.id == run_id, Run.execution_epoch == epoch,
                    Run.status == run.status, Run.cancel_requested.is_(False)]
                if expired:
                    conditions.append(or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= moment))
                changed = await session.execute(update(Run).where(*conditions).values(status="queued",
                    execution_epoch=epoch + 1, recovery_count=Run.recovery_count + 1, lease_owner=None,
                    lease_expires_at=None, heartbeat_at=None, job_id=f"run-{run_id}-e{epoch + 1}")
                    .execution_options(synchronize_session=False))
                if not changed.rowcount:
                    return False
                await session.refresh(run)
                previous = (await session.scalars(select(Outbox).where(Outbox.run_id == run_id,
                    Outbox.event_type == "dispatch_run").order_by(Outbox.created_at))).all()
                deadlines = [item.dispatch_deadline_at for item in previous if item.dispatch_deadline_at]
                deadline = min(deadlines) if deadlines else ((previous[0].created_at if previous else moment)
                    + timedelta(seconds=self.app.state.settings.dispatch_deadline_seconds))
                for intent in previous:
                    if intent.status not in {"exhausted", "cancelled"}:
                        intent.status = "superseded"
                session.add(Outbox(id=uid(), run_id=run_id, event_type="dispatch_run", status="pending",
                    payload={"run_id": run_id, "execution_epoch": run.execution_epoch}, available_at=moment,
                    attempts=max((item.attempts for item in previous), default=0), dispatch_deadline_at=deadline))
                session.add(Issue(id=uid(), run_id=run_id, user_id=run.user_id, subsystem="worker",
                    issue_type="recovery", issue_code="WORKER_LEASE_EXPIRED" if expired else "QUEUE_JOB_LOST",
                    severity="warning", metadata_json={"execution_epoch": run.execution_epoch, "recovery_count": run.recovery_count}))
                await event(session, run, "module_progress", {"phase": "worker_recovering", "execution_epoch": run.execution_epoch})
                if expired:
                    await signal_alert(session, "WORKER_LEASE_EXPIRED", "", run,
                        metadata={"execution_epoch": run.execution_epoch, "recovery_count": run.recovery_count})
                await session.commit()
        if exhausted_epoch is not None:
            message = "生成超过累计时间预算，请重试" if exhausted_code == "WORKFLOW_TIMEOUT" else "任务恢复次数已达到上限，请重新生成"
            await WorkflowExecutor(self.app).fail(run_id, exhausted_code, message,
                expected_epoch=exhausted_epoch, expired_only=True)
            if exhausted_code == "WORKER_RECOVERY_EXHAUSTED":
                async with self.app.state.db.session_factory() as session:
                    run, _ = await lock_run_with_draft(session, run_id)
                    if run and run.status == "failed" and run.error_code == exhausted_code:
                        await signal_alert(session, "WORKER_RECOVERY_EXHAUSTED", "", run, severity="error",
                            metadata={"execution_epoch": run.execution_epoch, "recovery_count": run.recovery_count})
                        await session.commit()
            return False
        await self.notify(run_id)
        return True

    async def reconcile(self):
        async with self.app.state.db.session_factory() as session:
            moment = await database_now(session)
            stale = (await session.execute(select(Run.id, Run.execution_epoch).where(Run.status == "running",
                or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= moment)).limit(100))).all()
        for rid, epoch in stale:
            await self.recover_run(rid, expected_epoch=epoch)
        async with self.app.state.db.session_factory() as session:
            moment = await database_now(session)
            cleanup = (await session.scalars(select(Outbox.run_id).join(Run, Run.id == Outbox.run_id)
                .where(Outbox.event_type == "dispatch_run",
                    or_(Run.status.not_in(["queued", "running"]),
                        (Run.status == "running") & (Outbox.status != "published"), Run.cancel_requested.is_(True)),
                    Outbox.status.in_(["pending", "dispatching", "checking", "published"]))
                .distinct().limit(100))).all()
            due_ids = (await session.scalars(select(Outbox.run_id).join(Run, Run.id == Outbox.run_id)
                .where(Outbox.event_type == "dispatch_run", Run.status == "queued",
                    Outbox.status.in_(["pending", "published", "dispatching", "checking"]), Outbox.available_at <= moment)
                .order_by(Outbox.available_at, Outbox.created_at, Outbox.id).limit(100))).all()
        for rid in cleanup:
            await self._reserve(rid)
        for rid in dict.fromkeys(due_ids):
            await self.submit(rid, recover=True)
        async with self.app.state.db.session_factory() as session:
            moment = await database_now(session)
            intents = (await session.scalars(select(Outbox).where(Outbox.event_type != "dispatch_run",
                Outbox.status == "pending", Outbox.available_at <= moment)
                .order_by(Outbox.available_at, Outbox.created_at).limit(100).with_for_update())).all()
            for intent in intents:
                try:
                    async with asyncio.timeout(self.app.state.settings.dispatch_timeout_seconds):
                        if intent.event_type == "delete_storage":
                            for key in intent.payload.get("keys", []):
                                await asyncio.to_thread(self.app.state.storage.delete, key)
                        else:
                            if self.redis is None:
                                raise RuntimeError("domain event transport unavailable")
                            receipt = await self.redis.xadd("lesson:domain-events", {"event_id": intent.id, "type": intent.event_type,
                                "payload": json.dumps(intent.payload)}, maxlen=100000, approximate=True)
                            if not receipt:
                                raise RuntimeError("domain event acknowledgement missing")
                    intent.status = "published"
                except Exception:
                    intent.available_at = moment + timedelta(seconds=self.retry_delay(intent.attempts + 1))
                    intent.last_error_code, intent.last_error_at = "OUTBOX_UNAVAILABLE", moment
                    recovery_log("outbox_retry_scheduled", code="OUTBOX_UNAVAILABLE", run_id=intent.run_id)
                intent.attempts += 1
            await session.commit()
        await scan_alerts(self.app)

    async def close(self):
        await self.stop_maintenance()
        if self.tasks:
            _, pending = await asyncio.wait(self.tasks, timeout=self.app.state.settings.run_timeout_seconds + 5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self.redis:
            await self.redis.aclose()
