"""Use cases own transactions, version pointers and run terminal states."""

import asyncio
import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.inspection import inspect

from app.ai.observability import observe_step, step
from app.core.errors import AppError
from app.db.models import (
    Blueprint,
    CallLog,
    Draft,
    Event,
    Export,
    Issue,
    KnowledgeDocument,
    ModuleVersion,
    Outbox,
    PersonalLesson,
    PolishMessage,
    PolishSession,
    QualityResult,
    Run,
    RunCheckpoint,
    Snapshot,
)

TERMINAL = {"completed", "failed", "cancelled"}
FOREGROUND = {"one_click", "interactive_step", "module_polish_message", "reflection_polish_message"}


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC)


async def database_now(session):
    """Lease time is shared across processes and never trusts a worker host's clock."""
    if session.bind.dialect.name == "postgresql":
        # CURRENT_TIMESTAMP is the transaction start time, which may precede a long row-lock wait.
        value = await session.scalar(select(func.clock_timestamp()))
    elif session.bind.dialect.name == "sqlite":
        value = datetime.fromisoformat(await session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))
    else:
        value = await session.scalar(select(func.current_timestamp()))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def lock_matching_run(session, *conditions):
    """Acquire a real lock without creating a throwaway PostgreSQL row version.

    A second UPDATE of that version can force FK rechecks and invert Draft→Run
    ordering through a parent KEY SHARE lock. SQLite needs a no-op write because
    its FOR UPDATE is ignored; PostgreSQL uses the same lock strength as writers.
    """
    if session.bind.dialect.name == "postgresql":
        return await session.scalar(select(Run.id).where(*conditions).with_for_update()) is not None
    result = await session.execute(update(Run).where(*conditions).values(heartbeat_at=Run.heartbeat_at)
        .execution_options(synchronize_session=False))
    return bool(result.rowcount)


class LeaseLost(Exception):
    """An obsolete attempt must exit without writing a terminal state for its successor."""


@dataclass(frozen=True)
class ExecutionLease:
    run_id: str
    epoch: int
    owner: str


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def asset_identity(runtime):
    assets = getattr(runtime, "assets", None)
    identity = assets.fingerprint() if assets and hasattr(assets, "fingerprint") else {}
    return {key: value for key, value in identity.items() if key != "consumed"}


def serialize(obj):
    if obj is None:
        return None
    return {attr.key: (value.isoformat() if isinstance(value := getattr(obj, attr.key), datetime) else value)
            for attr in inspect(obj).mapper.column_attrs}


def input_of(draft):
    return {name: getattr(draft, name) for name in (
        "topic", "school_stage", "grade", "subject", "lesson_type", "total_duration_min", "requirements", "generation_mode"
    )} | {"style_intent": draft.style_intent_snapshot, "knowledge_context": draft.knowledge_context_snapshot}


async def owned(session, model, object_id, user, *, lock=False):
    statement = select(model).where(model.id == object_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    obj = await session.scalar(statement)
    owner = getattr(obj, "user_id", None)
    if obj is None or owner != user.id or getattr(obj, "deleted_at", None):
        raise AppError("NOT_FOUND", "未找到该资源", 404)
    return obj


async def active_run(session, draft_id):
    return await session.scalar(select(Run).where(
        Run.draft_id == draft_id, Run.status.in_(["queued", "running"]),
        Run.workflow_type.in_(FOREGROUND)))


async def lock_run_with_draft(session, run_id):
    """All cross-aggregate writers lock Draft before Run to avoid a delete/commit deadlock."""
    initial = await session.get(Run, run_id)
    if not initial:
        return None, None
    draft = await session.scalar(select(Draft).where(Draft.id == initial.draft_id)
        .with_for_update().execution_options(populate_existing=True)) if initial.draft_id else None
    run = await session.scalar(select(Run).where(Run.id == run_id)
        .with_for_update().execution_options(populate_existing=True))
    return run, draft


async def event(session, run, kind, payload=None):
    from app.services.contracts import stream_event_validator
    # Allocate the sequence in the same transaction as its fact, across processes.
    sequence = await session.scalar(update(Run).where(Run.id == run.id)
        .values(sequence=Run.sequence + 1).returning(Run.sequence))
    run.sequence = sequence
    stream_event_validator().validate({"run_id": run.id, "sequence": sequence,
                                       "event_type": kind, "payload": payload or {}})
    session.add(Event(id=uid(), run_id=run.id, sequence=sequence,
                      event_type=kind, payload=payload or {}))


async def finish(session, run, status, *, result=None, code=None, message=None, degraded=False):
    if run.status in TERMINAL:
        return False
    run.status = status
    run.finished_at = now()
    run.result = result or {}
    run.result_status = {"completed": "success", "failed": "failed", "cancelled": "cancelled"}[status]
    if status == "completed" and degraded:
        run.result_status = "success_with_warnings"
    run.error_code, run.error_message = code, message
    run.lease_owner, run.lease_expires_at = None, None
    await event(session, run, {"completed": "run_completed", "failed": "run_failed", "cancelled": "run_cancelled"}[status], {
        "run_id": run.id, "result": result or {}, "error_code": code, "message": message,
        "state_preserved": status != "completed", "temporary_content_available": run.sequence > 1,
        "failure_display_mode": "content_with_warning" if status == "failed" else None,
    })
    await record_sample(session, run, status, degraded=degraded)
    return True


async def record_sample(session, run, status, *, degraded=False):
    """Atomic hourly denominators distinguish business outcomes from retry/issue facts."""
    hour = now().replace(minute=0, second=0, microsecond=0)
    elapsed = max(0, int((now() - (run.started_at or run.created_at or now())).total_seconds() * 1000))
    counts = {"sample_count": 1, "success_count": int(status == "completed"),
        "failure_count": int(status == "failed"), "cancelled_count": int(status == "cancelled"),
        "degraded_count": int(degraded), "slow_count": int(elapsed >= 60000), "duration_ms_sum": elapsed,
        "observation_count": 1}
    values = {"id": uid(), "user_id": run.user_id, "run_id": None, "record_kind": "sample",
        "sample_space": run.workflow_type, "sample_key": f"{run.user_id}:{run.workflow_type}:{hour.isoformat()}",
        "hour_bucket": hour, "subsystem": "workflow", "issue_type": "sample", "issue_code": "WORKFLOW_SAMPLE",
        "severity": "info", "metadata_json": {}, "occurrence_count": 0, **counts}
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(Issue).values(**values)
    await session.execute(statement.on_conflict_do_update(index_elements=[Issue.sample_key],
        set_={key: getattr(Issue, key) + value for key, value in counts.items()}))


async def detail(session, draft):
    versions = {}
    ids = list((draft.current_module_version_map or {}).values())
    if ids:
        versions = {v.id: v for v in (await session.scalars(select(ModuleVersion).where(ModuleVersion.id.in_(ids)))).all()}
    modules = []
    for config in draft.modules:
        item = dict(config)
        mid = item["module_id"]
        vid = draft.current_module_version_map.get(mid)
        version = versions.get(vid)
        item.update(current_version_id=vid, state=draft.module_state_map.get(mid, {}),
                    teacher_view_content=version.teacher_view_content if version else "",
                    canonical_content=version.canonical_content if version else {})
        modules.append(item)
    data = serialize(draft)
    data["modules"] = modules
    running = await active_run(session, draft.id)
    data["active_run_id"] = running.id if running else None
    return data


def check_lock(draft, module_id, body):
    expected = (body.expected_content_version, body.expected_current_module_version_id,
                body.expected_module_config_version_id)
    actual = (draft.content_version, draft.current_module_version_map.get(module_id), draft.module_config_version_id)
    if actual != expected:
        raise AppError("VERSION_CONFLICT", "教案已更新，请刷新后重新操作", 409)


def config_for(draft, module_id):
    item = next((m for m in draft.modules if m["module_id"] == module_id and m.get("enabled", True)), None)
    if not item:
        raise AppError("MODULE_NOT_FOUND", "模块不存在或未启用", 404)
    return item


def invalidate(draft, changed_id):
    states = {k: dict(v) for k, v in draft.module_state_map.items()}
    changed_type = config_for(draft, changed_id)["module_type"]
    from app.ai.assets import module_registry
    registry = module_registry()
    entries = registry.get("modules", registry) if isinstance(registry, dict) else registry
    entries = list(entries.values()) if isinstance(entries, dict) else entries
    dependencies = {m["module_type"]: m.get("dependencies", []) for m in entries}
    affected = {changed_type}
    for _ in range(len(draft.modules)):
        for m in draft.modules:
            if set(dependencies.get(m["module_type"], [])).intersection(affected):
                affected.add(m["module_type"])
    for m in draft.modules:
        mid = m["module_id"]
        if mid != changed_id and m["module_type"] in affected and draft.current_module_version_map.get(mid):
            states[mid]["freshness_status"] = "stale"
            states[mid]["freshness"] = "stale"
            states[mid]["semantic_invalidation_sources"] = [changed_id]
    draft.module_state_map = states


async def write_version(session, draft, module_id, content, *, canonical=None, source="ai_initial",
                        confirmed=False, dependencies=None, instruction=None, stage=None):
    previous_id = draft.current_module_version_map.get(module_id)
    previous = await session.get(ModuleVersion, previous_id) if previous_id else None
    version = ModuleVersion(
        id=uid(), draft_id=draft.id, module_id=module_id,
        version_no=(previous.version_no + 1 if previous else 1), parent_version_id=previous_id,
        canonical_content=canonical or {"markdown": content}, teacher_view_content=content,
        teacher_view_format="markdown", source=source, lifecycle_status="committed",
        confirmation_status="confirmed" if confirmed else "unconfirmed",
        actual_dependency_version_map=dependencies or {}, hard_gate_status="passed",
        teacher_instruction=instruction,
    )
    session.add(version)
    draft.current_module_version_map = {**draft.current_module_version_map, module_id: version.id}
    state = {**draft.module_state_map.get(module_id, {}), "generation_status": "ready",
        "confirmation_status": version.confirmation_status, "current_version_id": version.id,
        "confirmed_version_id": version.id if confirmed else None,
        "freshness_status": "fresh", "freshness": "fresh", "workflow_stage": stage or "complete"}
    if canonical and "outline" in canonical:
        state["outline"] = canonical["outline"]
    if stage == "outline":
        state["outline_confirmed"] = False
    draft.module_state_map = {**draft.module_state_map, module_id: state}
    draft.content_version += 1
    draft.updated_at = now()
    session.add(Outbox(id=uid(), run_id=None, event_type="module_version_committed", status="pending",
                       payload={"draft_id": draft.id, "module_id": module_id, "version_id": version.id}))
    await session.flush()
    return version


async def assemble(session, draft):
    await session.flush()
    from app.core.catalog import GRADES, SUBJECTS
    grade_label = next((entry["label"] for entry in GRADES if entry["value"] == draft.grade), draft.grade)
    subject_label = next((entry["label"] for entry in SUBJECTS if entry["value"] == draft.subject), draft.subject)
    warnings, lines = [], [f"# {draft.topic}", f"{grade_label} · {subject_label} · {draft.total_duration_min} 分钟"]
    for module in draft.modules:
        if not module.get("enabled", True):
            continue
        mid = module["module_id"]
        version = await session.get(ModuleVersion, draft.current_module_version_map.get(mid))
        if not version:
            warnings.append(f"{module['display_name']}尚未生成")
            continue
        if draft.module_state_map.get(mid, {}).get("freshness_status") in {"stale", "unknown"}:
            warnings.append(f"{module['display_name']}需要复核上游修改")
        body = re.sub(r"\A\s*#{1,3}\s+" + re.escape(module["display_name"]) + r"\s*\n+", "", version.teacher_view_content, count=1)
        lines += [f"## {module['display_name']}", body]
    content = "\n\n".join(lines)
    snapshot = Snapshot(id=uid(), draft_id=draft.id, module_version_map=dict(draft.current_module_version_map),
                        content=content, content_hash=digest(content), warnings=warnings)
    session.add(snapshot)
    return snapshot


async def create_run(session, draft, user, workflow, payload, key, *, logical_payload=None, dispatch_required=True):
    hashed = digest({"workflow": workflow, "draft_id": draft.id,
                     "payload": payload if logical_payload is None else logical_payload})
    old = await session.scalar(select(Run).where(Run.user_id == user.id, Run.idempotency_key == key))
    if old:
        if old.input_hash != hashed:
            raise AppError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求", 409)
        return old
    if workflow in FOREGROUND and await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "教案已有运行中的任务", 409)
    from app.tasks.queue import route_for
    queue_name = route_for(workflow)
    rid = uid()
    run = Run(id=rid, draft_id=draft.id, user_id=user.id, tenant_id=user.tenant_id,
              workflow_type=workflow, idempotency_key=key, input_hash=hashed, payload=payload,
              status="queued", sequence=0, result={}, queue_name=queue_name, job_id=f"run-{rid}")
    session.add(run)
    await session.flush()
    if dispatch_required:
        session.add(Outbox(id=uid(), run_id=rid, event_type="dispatch_run", payload={"run_id": rid, "execution_epoch": 0}, status="pending"))
    if workflow in {"one_click", "interactive_step"}:
        draft.status, draft.completion_status = "generating", "generating"
    await session.flush()
    return run


async def record_calls(session, run, calls):
    for call in calls or []:
        fallback = call.get("fallback", call.get("fallback_used", False))
        outcome = call.get("status", call.get("call_outcome", "success"))
        session.add(CallLog(id=uid(), run_id=run.id,
            component_name=call.get("component_name", "lesson_runtime"),
            provider=call.get("provider", call.get("llm_provider", "unknown")),
            model_name=call.get("model_name", call.get("model", "unknown")),
            prompt_version=call.get("prompt_version", "v1"), elapsed_ms=call.get("elapsed_ms", 0),
            prompt_tokens=call.get("prompt_tokens", 0), completion_tokens=call.get("completion_tokens", 0),
            fallback=fallback, status=outcome,
            error_code=call.get("error_code"), metadata_json={"span_id": call.get("span_id", uid()),
                "parent_span_id": call.get("parent_span_id"), "assets": call.get("assets", {}),
                "attempts": call.get("attempts", []), "primary_attempt_outcome": call.get("primary_attempt_outcome"),
                "is_demo": call.get("is_demo", False), "call_id": call.get("call_id"),
                "execution_epoch": run.execution_epoch, "validation_outcome": call.get("validation_outcome"),
                "validation_codes": call.get("validation_codes", [])}))
        if fallback or call.get("error_code"):
            session.add(Issue(id=uid(), run_id=run.id, user_id=run.user_id, subsystem="llm",
                issue_type="fallback" if fallback else "provider_failure",
                issue_code=call.get("error_code") or "FALLBACK_USED", severity="warning", metadata_json={}))


class WorkflowExecutor:
    def __init__(self, app):
        self.app = app
        self.lease = None
        self.lease_lost = False

    async def claim(self, run_id, expected_epoch=0):
        owner = uid()
        async with self.app.state.db.session_factory() as session:
            # Acquire the write lock before sampling time: a lock wait must not consume the new lease.
            locked = await lock_matching_run(session, Run.id == run_id, Run.status == "queued",
                Run.execution_epoch == expected_epoch)
            if not locked:
                return False
            moment = await database_now(session)
            claimed = await session.execute(update(Run).where(Run.id == run_id, Run.status == "queued",
                Run.execution_epoch == expected_epoch).values(status="running", lease_owner=owner,
                    lease_expires_at=moment + timedelta(seconds=self.app.state.settings.worker_lease_seconds),
                    heartbeat_at=moment, started_at=func.coalesce(Run.started_at, moment))
                    .execution_options(synchronize_session=False))
            await session.commit()
            if not claimed.rowcount:
                return False
        self.lease = ExecutionLease(run_id, expected_epoch, owner)
        return True

    async def lock_execution(self, session, run_id):
        """Draft first, then a conditional Run write: works on PG and SQLite alike."""
        lease = self.lease
        if lease is None or lease.run_id != run_id:
            raise LeaseLost()
        run, draft = await lock_run_with_draft(session, run_id)
        locked = await lock_matching_run(session, Run.id == run_id, Run.status == "running",
            Run.execution_epoch == lease.epoch, Run.lease_owner == lease.owner,
            Run.cancel_requested.is_(False))
        if not locked:
            raise LeaseLost()
        moment = await database_now(session)
        guarded = await session.execute(update(Run).where(Run.id == run_id, Run.status == "running",
            Run.execution_epoch == lease.epoch, Run.lease_owner == lease.owner,
            Run.cancel_requested.is_(False), Run.lease_expires_at > moment)
            .values(heartbeat_at=Run.heartbeat_at).execution_options(synchronize_session=False))
        if not guarded.rowcount or (draft and draft.deleted_at):
            raise LeaseLost()
        await session.refresh(run)
        return run, draft

    async def renew_lease(self):
        if self.lease is None:
            return False
        lease = self.lease
        async with self.app.state.db.session_factory() as session:
            # Time must be read after the lock is actually acquired. Even clock_timestamp()
            # sampled before a blocked UPDATE could revive a lease that expired during its wait.
            locked = await lock_matching_run(session, Run.id == lease.run_id,
                Run.status == "running", Run.execution_epoch == lease.epoch, Run.lease_owner == lease.owner,
                Run.cancel_requested.is_(False))
            if not locked:
                return False
            moment = await database_now(session)
            changed = await session.execute(update(Run).where(Run.id == lease.run_id,
                Run.status == "running", Run.execution_epoch == lease.epoch, Run.lease_owner == lease.owner,
                Run.cancel_requested.is_(False), Run.lease_expires_at > moment).values(
                    heartbeat_at=moment, lease_expires_at=moment + timedelta(seconds=self.app.state.settings.worker_lease_seconds))
                    .execution_options(synchronize_session=False))
            await session.commit()
            return bool(changed.rowcount)

    async def abandon(self):
        if self.lease is None:
            return
        async with self.app.state.db.session_factory() as session:
            moment = await database_now(session)
            await session.execute(update(Run).where(Run.id == self.lease.run_id, Run.status == "running",
                Run.execution_epoch == self.lease.epoch, Run.lease_owner == self.lease.owner)
                .values(lease_expires_at=moment - timedelta(seconds=1)).execution_options(synchronize_session=False))
            await session.commit()

    async def heartbeat(self, task):
        while True:
            await asyncio.sleep(self.app.state.settings.worker_heartbeat_seconds)
            try:
                renewed = await self.renew_lease()
            except Exception:
                renewed = False
            if not renewed:
                self.lease_lost = True
                task.cancel()
                return

    @observe_step("checkpoint.save", run_type="tool")
    async def save_checkpoint(self, run_id, key, payload, fingerprint):
        async with self.app.state.db.session_factory() as session:
            await self.lock_execution(session, run_id)
            existing = await session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id,
                RunCheckpoint.checkpoint_key == key))
            if existing is None:
                session.add(RunCheckpoint(id=uid(), run_id=run_id, checkpoint_key=key,
                    input_fingerprint=fingerprint, payload=payload))
            else:
                existing.input_fingerprint, existing.payload = fingerprint, payload
            await session.commit()

    @observe_step("checkpoint.load", run_type="tool")
    async def load_checkpoints(self, run_id, fingerprint):
        async with self.app.state.db.session_factory() as session:
            await self.lock_execution(session, run_id)
            rows = (await session.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id))).all()
            if any(row.input_fingerprint != fingerprint for row in rows):
                # Losing the old ledger would incorrectly grant another complete call/retry budget.
                raise AppError("CHECKPOINT_CONTEXT_CHANGED", "恢复上下文或受控资产已变更，请重新创建教案", 409)
            return {row.checkpoint_key: row.payload for row in rows}

    async def emit(self, run_id, kind, payload):
        async with self.app.state.db.session_factory() as session:
            run, _ = await self.lock_execution(session, run_id)
            await event(session, run, kind, payload)
            await session.commit()
        await self.app.state.dispatcher.notify(run_id)

    async def execute(self, run_id, expected_epoch=0):
        if self.lease is None:
            if not await self.claim(run_id, expected_epoch):
                return
        elif self.lease.run_id != run_id or self.lease.epoch != expected_epoch:
            raise LeaseLost()
        async with self.app.state.db.session_factory() as session:
            # Initial claim also checks queued cancellation/deletion before starting any work.
            run, draft = await lock_run_with_draft(session, run_id)
            moment = await database_now(session)
            valid = await session.execute(update(Run).where(Run.id == run_id, Run.status == "running",
                Run.execution_epoch == self.lease.epoch, Run.lease_owner == self.lease.owner,
                Run.lease_expires_at > moment).values(heartbeat_at=Run.heartbeat_at)
                .execution_options(synchronize_session=False))
            if not valid.rowcount:
                return
            await session.refresh(run)
            if (run.workflow_type not in {"document_parse", "knowledge_document_parse"} and (not draft or draft.deleted_at)) or run.cancel_requested:
                await finish(session, run, "cancelled")
                await session.commit()
                return
            await event(session, run, "run_started", {"run_id": run.id, "workflow_type": run.workflow_type})
            data, payload, workflow = input_of(draft) if draft else {}, dict(run.payload), run.workflow_type
            await session.commit()
        heartbeat = asyncio.create_task(self.heartbeat(asyncio.current_task()), name=f"lease-{run_id}-{expected_epoch}")
        try:
            if workflow == "export":
                return await self.export(run_id, payload)
            if workflow == "document_parse":
                return await self.parse_document(run_id, payload)
            if workflow == "knowledge_document_parse":
                from app.services.knowledge import parse_knowledge_document
                return await parse_knowledge_document(self, run_id, payload)
            if workflow == "lesson_internal_review":
                return await self.review(run_id, data)
            if workflow in {"module_polish_message", "reflection_polish_message"}:
                return await self.polish(run_id, data, payload)
            await self.generate(run_id, data, payload, workflow)
        except LeaseLost:
            return
        except asyncio.CancelledError:
            # Deadline and process shutdown are owned by the task boundary.
            raise
        except Exception as exc:
            # Provider exceptions may contain secrets; only controlled errors reach public facts.
            code = getattr(exc, "code", None) or "WORKFLOW_FAILED"
            message = ("模型调用结果尚未确认，已停止自动重发以避免重复计费，请核对服务商记录"
                if code == "LLM_OUTCOME_UNKNOWN" else "任务未完成，请稍后重试")
            await self.fail(run_id, code, message,
                            calls=getattr(exc, "calls", []), findings=getattr(exc, "findings", []))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def fail(self, run_id, code, message, *, cancelled=False, calls=None, findings=None, expected_epoch=None, expired_only=False):
        async with self.app.state.db.session_factory() as session:
            if self.lease is not None:
                try:
                    run, draft = await self.lock_execution(session, run_id)
                except LeaseLost:
                    return
            else:
                run, draft = await lock_run_with_draft(session, run_id)
                if not run or (expected_epoch is not None and run.execution_epoch != expected_epoch):
                    return
                moment = await database_now(session)
                if run.status != "queued" and not (expired_only and run.status == "running"
                    and (run.lease_expires_at is None or run.lease_expires_at <= moment)):
                    return
                eligible = Run.status == "queued"
                if expired_only:
                    eligible = or_(eligible, and_(Run.status == "running",
                        or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= moment)))
                guarded = await session.execute(update(Run).where(Run.id == run_id,
                    Run.execution_epoch == run.execution_epoch, eligible).values(heartbeat_at=Run.heartbeat_at)
                    .execution_options(synchronize_session=False))
                if not guarded.rowcount:
                    return
                await session.refresh(run)
            if not run or run.status in TERMINAL:
                return
            if not calls:
                budget = await session.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id,
                    RunCheckpoint.checkpoint_key == "budget"))
                if budget:
                    calls = [{**call, **({"call_outcome": "interrupted", "error_code": code}
                        if call.get("call_outcome") == "started" else {})} for call in budget.payload.get("calls", [])]
            status = "cancelled" if cancelled or run.cancel_requested else "failed"
            await finish(session, run, status, code=code, message=message)
            if draft and run.workflow_type in {"one_click", "interactive_step"} and not draft.deleted_at:
                draft.status = "cancelled" if status == "cancelled" else "failed_retryable"
                draft.completion_status = draft.status
            if run.workflow_type == "export":
                obj = await session.get(Export, run.payload["export_id"])
                if obj:
                    obj.status, obj.error_message = "failed", message
            if run.workflow_type == "document_parse":
                obj = await session.get(PersonalLesson, run.payload["document_id"])
                if obj:
                    obj.status, obj.error_message = "failed", message
            if run.workflow_type == "knowledge_document_parse":
                obj = await session.get(KnowledgeDocument, run.payload.get("document_id"))
                if obj:
                    obj.status, obj.error_message = "failed", message
            if run.workflow_type == "lesson_internal_review" and draft:
                draft.semantic_review_status = "failed"
            session.add(Issue(id=uid(), run_id=run.id, user_id=run.user_id, subsystem="workflow",
                              issue_type="cancelled" if cancelled else "failure", issue_code=code,
                              severity="warning" if cancelled else "error", metadata_json={
                                  "workflow_type": run.workflow_type, "queue_name": run.queue_name,
                                  "job_id": run.job_id, "execution_epoch": run.execution_epoch, "stage": run.payload.get("stage"),
                                  "module_id": run.payload.get("module_id")}))
            await record_calls(session, run, calls)
            if code == "LLM_OUTCOME_UNKNOWN":
                from app.services.recovery import signal_alert
                await signal_alert(session, "LLM_OUTCOME_UNKNOWN", "", run, severity="warning",
                    metadata={"execution_epoch": run.execution_epoch})
            if findings and draft:
                session.add(QualityResult(id=uid(), run_id=run.id, draft_id=draft.id,
                    module_id=run.payload.get("module_id"), gate="module_quality", status="blocked",
                    findings=findings, content_hash=""))
            await session.commit()
        await self.app.state.dispatcher.notify(run_id)

    @observe_step("workflow.generate")
    async def generate(self, run_id, data, payload, workflow):
        async with self.app.state.db.session_factory() as session:
            run, draft = await self.lock_execution(session, run_id)
            modules = [m for m in draft.modules if m.get("enabled", True)]
            if workflow == "interactive_step":
                modules = [config_for(draft, payload["module_id"])]
            blueprint = await session.get(Blueprint, draft.current_blueprint_version_id) if draft.current_blueprint_version_id else None
            dependencies, dep_ids = {}, {}
            from app.ai.assets import module_registry
            registry = module_registry()
            selected_types = {m["module_type"] for m in modules}
            needed = set()
            for module_type in selected_types:
                item = registry.get(module_type, {}) if isinstance(registry, dict) else {}
                needed.update(item.get("dependencies", item.get("depends_on", [])))
            for mid, state in draft.module_state_map.items():
                vid = state.get("confirmed_version_id")
                if vid and (config_for(draft, mid)["module_type"] in needed or mid == payload.get("module_id")):
                    version = await session.get(ModuleVersion, vid)
                    dependencies[mid] = {"version_id": vid, "content": version.teacher_view_content,
                                         "module_type": config_for(draft, mid)["module_type"]}
                    dep_ids[mid] = vid
            outline = draft.module_state_map.get(payload.get("module_id"), {}).get("outline")
            fingerprint = digest({"inputs": data, "modules": modules, "dependencies": dependencies,
                "outline": outline, "module_config": draft.module_config_version_id, "assets": asset_identity(self.app.state.runtime),
                "stage": payload.get("stage"), "workflow": workflow, "blueprint": blueprint.payload if blueprint else None})
            await session.commit()
        resume = await self.load_checkpoints(run_id, fingerprint)
        from app.services.invocations import InvocationJournal
        journal = InvocationJournal.for_workflow(self, run_id, fingerprint)
        if self.lease.epoch:
            await self.emit(run_id, "module_progress", {"phase": "worker_recovering", "execution_epoch": self.lease.epoch})
            for module in modules:
                await self.emit(run_id, "content_delta", {"module_id": module["module_id"],
                    "delta": "", "replace_previous": True, "is_temporary": True})

        async def checkpoint_sink(key, item):
            await self.save_checkpoint(run_id, key, item, fingerprint)
        if workflow == "interactive_step":
            await self.emit(run_id, "structured_step_started", {"module_id": payload["module_id"], "stage": payload.get("stage")})
        style = data.get("style_intent") or {}
        await self.emit(run_id, "style_context_resolved", style)
        quality_started = False
        async def sink(kind, item):
            nonlocal quality_started
            aliases = {"module_delta": "content_delta", "module_completed": "module_done"}
            mapped = aliases.get(kind, kind)
            info = dict(item)
            if mapped == "content_delta":
                info.setdefault("delta", info.get("content", info.get("text", "")))
            if mapped == "module_done":
                info = {"module_id": info.get("module_id"), "is_temporary": True}
            if mapped == "style_context_resolved":
                return
            if mapped not in {"module_started", "module_done", "content_delta", "module_progress", "stage_started", "stage_done"}:
                info = {**info, "phase": {"blueprint_ready": "blueprint_generation_done"}.get(mapped, mapped)}
                mapped = "module_progress"
            if mapped == "module_progress" and info.get("phase") == "quality_check_and_repair_started":
                quality_started = True
            await self.emit(run_id, mapped, info)
        result = await self.app.state.runtime.generate(data, modules,
            blueprint=blueprint.payload if blueprint else None, dependencies=dependencies if workflow == "interactive_step" else None,
            stage=payload.get("stage"), outline=outline, event_sink=sink,
            checkpoint_sink=checkpoint_sink, resume=resume, invocation_store=journal)
        if not quality_started:
            await self.emit(run_id, "module_progress", {"phase": "quality_check_and_repair_started"})
        async with self.app.state.db.session_factory() as session:
            run, draft = await self.lock_execution(session, run_id)
            if result.get("blueprint") and not draft.current_blueprint_version_id:
                bp = Blueprint(id=uid(), draft_id=draft.id, payload=result["blueprint"], content_hash=digest(result["blueprint"]))
                session.add(bp)
                draft.current_blueprint_version_id = bp.id
            actual_ids = {m["module_id"] for m in result.get("modules", [])}
            if actual_ids != {m["module_id"] for m in modules}:
                raise AppError("INCOMPLETE_GENERATION", "生成结果缺少所选模块", 502)
            for module in result["modules"]:
                mid = module["module_id"]
                findings = module.get("findings", [])
                if any(f.get("severity") in {"error", "blocker"} or f.get("blocking") for f in findings):
                    raise AppError("QUALITY_BLOCKED", "生成结果未通过质量检查", 422)
                if workflow == "interactive_step" and draft.current_module_version_map.get(mid):
                    invalidate(draft, mid)
                await write_version(session, draft, mid, module["teacher_view_content"],
                    canonical=module.get("canonical_content"), confirmed=workflow == "one_click",
                    dependencies=dep_ids, stage=payload.get("stage"))
                session.add(QualityResult(id=uid(), run_id=run.id, draft_id=draft.id, module_id=mid,
                    gate="module_quality", status="passed", findings=findings, content_hash=digest(module["teacher_view_content"])))
                await event(session, run, "content_delta", {"module_id": mid, "delta": module["teacher_view_content"],
                    "content": module["teacher_view_content"], "replace_previous": True, "is_temporary": False})
                await event(session, run, "module_done", {"module_id": mid, "is_temporary": False,
                    "version_id": draft.current_module_version_map[mid]})
            await record_calls(session, run, result.get("calls"))
            await event(session, run, "module_progress", {"phase": "quality_check_and_repair_done"})
            draft.hard_gate_status = "passed"
            if workflow == "one_click":
                draft.status, draft.completion_status = "ready", "completed"
                snapshot = await assemble(session, draft)
                await event(session, run, "draft_ready", {"draft_id": draft.id, "snapshot_id": snapshot.id})
            else:
                draft.status, draft.completion_status = "creating", "writing"
                await event(session, run, "structured_step_ready", {"module_id": payload["module_id"], "stage": payload.get("stage")})
            degraded = any(call.get("fallback_used", call.get("fallback")) for call in result.get("calls", []))
            await journal.mark_applied(session)
            await finish(session, run, "completed", result={"draft_id": draft.id}, degraded=degraded)
            review_run = None
            if workflow == "one_click":
                review_run = await self.queue_review(session, draft, run)
            with step("business.commit", run_type="tool"):
                await session.commit()
        if review_run:
            await self.app.state.dispatcher.submit(review_run.id)
        await self.app.state.dispatcher.notify(run_id)

    async def queue_review(self, session, draft, source_run):
        if not self.app.state.settings.internal_review_enabled or self.app.state.settings.ai_provider == "mock":
            return None
        from app.db.models import User
        user = await session.get(User, source_run.user_id)
        draft.semantic_review_status = "queued"
        return await create_run(session, draft, user, "lesson_internal_review", {},
                                f"review-{draft.id}-{draft.content_version}")

    @observe_step("workflow.polish")
    async def polish(self, run_id, data, payload):
        content = payload["working_content"]
        if payload.get("scope") == "selection":
            content = content[payload["selection_start"]:payload["selection_end"]]
        async with self.app.state.db.session_factory() as session:
            run, _ = await self.lock_execution(session, run_id)
            polish = await session.get(PolishSession, payload["session_id"])
            message = await session.get(PolishMessage, payload["message_id"])
            if not polish or polish.user_id != run.user_id or polish.draft_id != run.draft_id or not message or message.session_id != polish.id:
                raise AppError("POLISH_SESSION_NOT_FOUND", "润色会话已失效", 404)
            fingerprint = digest({"workflow": run.workflow_type, "inputs": data, "payload": payload,
                "content": content, "scope": payload.get("scope", "module"), "instruction": payload["instruction"],
                "session": {"id": polish.id, "base_version_id": polish.base_version_id, "kind": polish.kind,
                    "module_id": polish.module_id, "selection_start": polish.selection_start, "selection_end": polish.selection_end},
                "assets": asset_identity(self.app.state.runtime)})
        resume = await self.load_checkpoints(run_id, fingerprint)

        from app.services.invocations import InvocationJournal
        journal = InvocationJournal.for_workflow(self, run_id, fingerprint)

        async def checkpoint_sink(key, item):
            await self.save_checkpoint(run_id, key, item, fingerprint)

        await self.emit(run_id, "message_started", {"session_id": payload["session_id"]})
        await self.emit(run_id, "module_started", {"module_id": payload.get("module_id"), "scope": payload.get("scope")})
        if self.lease.epoch:
            await self.emit(run_id, "content_delta", {"module_id": payload.get("module_id"),
                "delta": "", "replace_previous": True, "is_temporary": True})
        async def sink(kind, item):
            if kind == "content_delta":
                await self.emit(run_id, "content_delta", {**item, "module_id": payload.get("module_id")})
            elif kind == "module_progress":
                await self.emit(run_id, kind, {**item, "module_id": payload.get("module_id")})
        result = await self.app.state.runtime.polish(data, content, payload["instruction"],
            scope=payload.get("scope", "module"), event_sink=sink, checkpoint_sink=checkpoint_sink,
            resume=resume, invocation_store=journal)
        async with self.app.state.db.session_factory() as session:
            run, draft = await self.lock_execution(session, run_id)
            candidate = result["content"]
            polish = await session.get(PolishSession, payload["session_id"])
            message = await session.get(PolishMessage, payload["message_id"])
            polish.candidate = candidate
            message.candidate = candidate
            await record_calls(session, run, result.get("calls"))
            ready = {"candidate": candidate, "scope": payload.get("scope"), "base_version_id": polish.base_version_id,
                     "selection_start": polish.selection_start, "selection_end": polish.selection_end}
            await event(session, run, "content_delta", {"module_id": payload.get("module_id"), "delta": candidate,
                                                       "replace_previous": True, "is_temporary": False})
            await event(session, run, "module_done", {"module_id": payload.get("module_id"), "is_temporary": False})
            await event(session, run, "polish_ready", ready)
            await journal.mark_applied(session)
            await finish(session, run, "completed", result=ready,
                degraded=any(call.get("fallback_used", call.get("fallback")) for call in result.get("calls", [])))
            with step("business.commit", run_type="tool"):
                await session.commit()
        await self.app.state.dispatcher.notify(run_id)

    @observe_step("workflow.export")
    async def export(self, run_id, payload):
        from app.integrations.documents import DocumentAdapter
        async with self.app.state.db.session_factory() as session:
            await self.lock_execution(session, run_id)
            export = await session.get(Export, payload["export_id"])
            snapshot = await session.get(Snapshot, export.snapshot_id)
            draft = await session.get(Draft, export.draft_id)
            content, title, fmt = snapshot.content, draft.topic, export.format
        adapter = DocumentAdapter()
        file_bytes = await asyncio.to_thread(adapter.export, content, title, fmt)
        async with self.app.state.db.session_factory() as session:
            await self.lock_execution(session, run_id)
        key = await asyncio.to_thread(self.app.state.storage.put, file_bytes, suffix=f".{fmt}")
        saved = False
        try:
            # Readback failures require the same compensation as a failed database commit.
            stored = await asyncio.to_thread(self.app.state.storage.get, key)
            if stored != file_bytes or not stored:
                raise AppError("EXPORT_UNREADABLE", "导出文件不可读取", 502)
            async with self.app.state.db.session_factory() as session:
                run, _ = await self.lock_execution(session, run_id)
                export = await session.get(Export, payload["export_id"])
                export.status, export.file_path = "success", key
                warnings = getattr(adapter, "warnings", [])
                export.warnings = list(export.warnings or []) + list(warnings)
                if warnings:
                    session.add(Issue(id=uid(), run_id=run.id, user_id=run.user_id, subsystem="export",
                        issue_type="render_warning", issue_code="EXPORT_RENDER_WARNING", severity="warning",
                        metadata_json={"warning_count": len(warnings)}))
                await finish(session, run, "completed", result={"export_id": export.id}, degraded=bool(warnings))
                with step("business.commit", run_type="tool"):
                    await session.commit()
                saved = True
        finally:
            if not saved:
                with suppress(Exception):
                    await asyncio.to_thread(self.app.state.storage.delete, key)
        await self.app.state.dispatcher.notify(run_id)

    @observe_step("workflow.document_parse")
    async def parse_document(self, run_id, payload):
        from app.integrations.documents import DocumentAdapter
        from app.integrations.thumbnails import create_thumbnail
        async with self.app.state.db.session_factory() as session:
            run, _ = await self.lock_execution(session, run_id)
            document = await session.get(PersonalLesson, payload["document_id"])
            if document is None:
                await finish(session, run, "cancelled", code="DOCUMENT_DELETED", message="教案文档已删除")
                await session.commit()
                return
            filename, key = document.filename, document.file_path
        raw = await asyncio.to_thread(self.app.state.storage.get, key)
        parsed = await asyncio.to_thread(DocumentAdapter().parse_upload, filename, raw)
        thumbnail_key, thumbnail_kind, warnings = None, None, []
        try:
            png, thumbnail_kind, warnings = await asyncio.to_thread(create_thumbnail, filename, raw, text=parsed["text"])
            async with self.app.state.db.session_factory() as session:
                await self.lock_execution(session, run_id)
            thumbnail_key = await asyncio.to_thread(self.app.state.storage.put, png, suffix=".png", content_type="image/png")
        except LeaseLost:
            raise
        except Exception:
            warnings = ["thumbnail_unavailable"]
        saved = False
        try:
            saved = await self.commit_document(run_id, payload, parsed, thumbnail_key, thumbnail_kind, warnings)
        finally:
            if thumbnail_key and not saved:
                with suppress(Exception):
                    await asyncio.to_thread(self.app.state.storage.delete, thumbnail_key)
        await self.app.state.dispatcher.notify(run_id)

    async def commit_document(self, run_id, payload, parsed, thumbnail_key, thumbnail_kind, warnings):
        async with self.app.state.db.session_factory() as session:
            run, _ = await self.lock_execution(session, run_id)
            document = await session.scalar(select(PersonalLesson).where(PersonalLesson.id == payload["document_id"])
                .with_for_update().execution_options(populate_existing=True))
            if document is None:
                await finish(session, run, "cancelled")
            else:
                document.content, document.style_summary, document.status = parsed["text"], parsed["style_summary"], "ready"
                document.chunks = parsed.get("chunks", [])
                document.thumbnail_path, document.thumbnail_kind = thumbnail_key, thumbnail_kind
                for field in ("school_stage", "grade", "subject", "lesson_type"):
                    setattr(document, field, parsed.get("metadata", {}).get(field) or ("new_lesson" if field == "lesson_type" else None))
                if warnings:
                    session.add(Issue(id=uid(), run_id=run.id, user_id=run.user_id, subsystem="document",
                        issue_type="thumbnail_warning", issue_code="THUMBNAIL_PREVIEW_WARNING", severity="info",
                        metadata_json={"warnings": warnings}))
                await finish(session, run, "completed", result={"document_id": document.id}, degraded="thumbnail_unavailable" in warnings)
            await session.commit()
            return document is not None

    @observe_step("workflow.review")
    async def review(self, run_id, data):
        async with self.app.state.db.session_factory() as session:
            run, draft = await self.lock_execution(session, run_id)
            modules = (await detail(session, draft))["modules"]
            fingerprint = digest({"workflow": "lesson_internal_review", "inputs": data,
                "modules": modules, "version_map": draft.current_module_version_map,
                "assets": asset_identity(self.app.state.runtime)})
        resume = await self.load_checkpoints(run_id, fingerprint)

        from app.services.invocations import InvocationJournal
        journal = InvocationJournal.for_workflow(self, run_id, fingerprint)

        async def checkpoint_sink(key, item):
            await self.save_checkpoint(run_id, key, item, fingerprint)

        async def sink(kind, item):
            if kind == "module_progress":
                await self.emit(run_id, kind, item)

        result = await self.app.state.runtime.review(data, modules,
            checkpoint_sink=checkpoint_sink, resume=resume, event_sink=sink, invocation_store=journal)
        async with self.app.state.db.session_factory() as session:
            run, draft = await self.lock_execution(session, run_id)
            draft.semantic_review_status = "done"
            session.add(QualityResult(id=uid(), run_id=run.id, draft_id=draft.id, module_id=None,
                gate="cross_module_review", status="done", findings=result.get("findings", []), content_hash=digest(modules)))
            await record_calls(session, run, result.get("calls"))
            await journal.mark_applied(session)
            await finish(session, run, "completed", result={"reviewed": True})
            with step("business.commit", run_type="tool"):
                await session.commit()
        await self.app.state.dispatcher.notify(run_id)
