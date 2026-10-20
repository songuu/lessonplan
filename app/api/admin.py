from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import admin_user
from app.db.models import (
    Audit,
    CallLog,
    Draft,
    Event,
    Export,
    Issue,
    LLMInvocation,
    Outbox,
    RecoveryAlert,
    Run,
    RunCheckpoint,
    User,
)
from app.db.session import get_session
from app.services.lessons import (
    TERMINAL,
    database_now,
    digest,
    finish,
    lock_matching_run,
    lock_run_with_draft,
    serialize,
    uid,
)

router = APIRouter(prefix="/admin")
Admin = Annotated[User, Depends(admin_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


async def audited(session, user, action, resource=None):
    session.add(Audit(id=uid(), user_id=user.id, action=action, resource_id=resource, metadata_json={}))
    await session.commit()


@router.get("/overview")
async def overview(user: Admin, session: Session):
    result = {}
    for name, model in [("users", User), ("drafts", Draft), ("runs", Run), ("exports", Export), ("issues", Issue)]:
        query = select(func.count()).select_from(model)
        if model is Issue:
            query = query.where(Issue.record_kind == "issue")
        result[name] = await session.scalar(query)
    result["running"] = await session.scalar(select(func.count()).select_from(Run).where(Run.status.in_(["queued", "running"])))
    await audited(session, user, "view_overview")
    return {"data": result}


@router.get("/users")
async def users(user: Admin, session: Session, offset: int = 0):
    items = (await session.scalars(select(User).order_by(User.created_at.desc()).limit(100).offset(max(offset, 0)))).all()
    result = [{k: v for k, v in serialize(item).items() if k not in {"password_hash", "preferences", "profile"}} for item in items]
    await audited(session, user, "list_users")
    return {"data": result}


class UserUpdate(BaseModel):
    is_active: bool | None = None
    role: Literal["admin", "teacher"] | None = None


@router.patch("/users/{user_id}")
async def user_update(user_id: str, body: UserUpdate, user: Admin, session: Session):
    item = await session.get(User, user_id)
    if not item:
        raise AppError("NOT_FOUND", "用户不存在", 404)
    if item.id == user.id and (body.is_active is False or body.role == "teacher"):
        raise AppError("CANNOT_DISABLE_SELF", "不能移除自己的管理权限", 409)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(item, key, value)
    await audited(session, user, "update_user", user_id)
    return {"data": {"id": item.id, "role": item.role, "is_active": item.is_active}}


@router.get("/runs")
async def runs(user: Admin, session: Session, status: str | None = None):
    query = select(Run)
    if status:
        query = query.where(Run.status == status)
    items = (await session.scalars(query.order_by(Run.created_at.desc()).limit(100))).all()
    result = [{k: v for k, v in serialize(item).items() if k not in {"payload", "result"}} for item in items]
    await audited(session, user, "list_runs")
    return {"data": result}


@router.get("/issues")
async def issues(user: Admin, session: Session):
    items = (await session.scalars(select(Issue).where(Issue.record_kind == "issue").order_by(Issue.created_at.desc()).limit(100))).all()
    await audited(session, user, "list_issues")
    return {"data": [serialize(item) for item in items]}


@router.get("/audit")
async def audit(user: Admin, session: Session):
    items = (await session.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(100))).all()
    await audited(session, user, "list_audit")
    return {"data": [serialize(item) for item in items]}


RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
AlertStatus = Literal["open", "acknowledged", "resolved"]
_METADATA_KEYS = {
    "execution_epoch", "recovery_count", "attempts", "next_attempt_at", "queue_name",
    "oldest_age_seconds", "queued_count", "error_code", "available_at", "deadline_at",
    "phase", "module_id", "checkpoint_key", "attempt", "max_attempts", "code",
}


def _safe_metadata(value):
    # Operational views never inherit arbitrary provider logs or teacher content.
    return {key: item[:200] if isinstance(item, str) else item
            for key, item in (value or {}).items() if key in _METADATA_KEYS
            and (item is None or isinstance(item, (str, int, float, bool)))}


def _alert_view(alert):
    return {**{key: value for key, value in serialize(alert).items() if key != "metadata_json"},
            "metadata_json": _safe_metadata(alert.metadata_json)}


async def _current_intent(session, run, *, lock=False):
    query = select(Outbox).where(Outbox.run_id == run.id, Outbox.event_type == "dispatch_run")
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    intents = (await session.scalars(query.order_by(Outbox.created_at.desc(), Outbox.id.desc()))).all()
    return next((item for item in intents if item.payload.get("execution_epoch", 0) == run.execution_epoch), None)


async def _run_summaries(session, records, settings, moment):
    if not records:
        return []
    ids = [record.id for record in records]
    topics = dict((await session.execute(select(Draft.id, Draft.topic).where(
        Draft.id.in_({record.draft_id for record in records if record.draft_id})))).all())
    counts = dict((await session.execute(select(RunCheckpoint.run_id, func.count()).where(
        RunCheckpoint.run_id.in_(ids), RunCheckpoint.checkpoint_key.like("module:%"))
        .group_by(RunCheckpoint.run_id))).all())
    intents = (await session.scalars(select(Outbox).where(Outbox.run_id.in_(ids),
        Outbox.event_type == "dispatch_run").order_by(Outbox.created_at.desc(), Outbox.id.desc()))).all()
    by_epoch = {}
    for item in intents:
        by_epoch.setdefault((item.run_id, item.payload.get("execution_epoch", 0)), item)
    rows = []
    for run in records:
        intent = by_epoch.get((run.id, run.execution_epoch))
        within_deadline = not intent or not intent.dispatch_deadline_at or intent.dispatch_deadline_at > moment
        attempts_available = not intent or intent.attempts < settings.dispatch_max_attempts
        expired = run.status == "running" and (not run.lease_expires_at or run.lease_expires_at <= moment)
        elapsed = max(0, (moment - run.started_at).total_seconds()) if run.started_at else 0
        retry = not run.cancel_requested and within_deadline and attempts_available and (
            (run.status == "queued" and intent is not None and intent.status == "pending") or
            (expired and run.recovery_count < settings.worker_recovery_limit and elapsed < settings.run_timeout_seconds))
        rows.append({
            **{key: value for key, value in serialize(run).items() if key in {
                "id", "draft_id", "user_id", "workflow_type", "status", "error_code", "execution_epoch",
                "recovery_count", "heartbeat_at", "lease_expires_at", "created_at", "started_at", "finished_at"}},
            "topic": topics.get(run.draft_id), "checkpoint_count": counts.get(run.id, 0),
            "dispatch": ({"id": intent.id, "status": intent.status, "attempts": intent.attempts,
                "next_attempt_at": intent.available_at.isoformat(),
                "last_attempt_at": intent.last_attempt_at.isoformat() if intent.last_attempt_at else None,
                "last_error_code": intent.last_error_code,
                "deadline_at": intent.dispatch_deadline_at.isoformat() if intent.dispatch_deadline_at else None} if intent else None),
            "can_retry": bool(retry), "can_cancel": run.status not in TERMINAL and not run.cancel_requested,
        })
    return rows


@router.get("/recovery-overview")
async def recovery_overview(request: Request, user: Admin, session: Session):
    moment = await database_now(session)
    counts = dict((await session.execute(select(Run.status, func.count()).group_by(Run.status))).all())
    retry_pending = await session.scalar(select(func.count(func.distinct(Run.id))).select_from(Run).join(
        Outbox, Outbox.run_id == Run.id).where(Run.status == "queued", Outbox.event_type == "dispatch_run",
            Outbox.status == "pending", Outbox.payload["execution_epoch"].as_integer() == Run.execution_epoch))
    expired = await session.scalar(select(func.count()).select_from(Run).where(Run.status == "running",
        or_(Run.lease_expires_at.is_(None), Run.lease_expires_at <= moment)))
    alerts = await session.scalar(select(func.count()).select_from(RecoveryAlert).where(RecoveryAlert.status != "resolved"))
    return {"data": {"queued": counts.get("queued", 0), "running": counts.get("running", 0),
        "retry_pending": retry_pending, "expired_leases": expired, "open_alerts": alerts, "failed": counts.get("failed", 0)}}


@router.get("/recovery-runs")
async def recovery_runs(request: Request, user: Admin, session: Session,
        status: RunStatus | None = None, search: str = Query(default="", max_length=120),
        offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=20, ge=1, le=100),
        attention_only: bool = False):
    moment = await database_now(session)
    conditions = []
    if status:
        conditions.append(Run.status == status)
    if search.strip():
        text = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(or_(Run.id.ilike(f"%{text}%", escape="\\"), Draft.topic.ilike(f"%{text}%", escape="\\")))
    if attention_only:
        conditions.append(or_(Run.status == "failed", Run.recovery_count > 0,
            Run.id.in_(select(RecoveryAlert.run_id).where(RecoveryAlert.status != "resolved"))))
    query = select(Run).outerjoin(Draft, Draft.id == Run.draft_id).where(*conditions)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    records = (await session.scalars(query.order_by(Run.created_at.desc(), Run.id.desc()).offset(offset).limit(limit))).all()
    return {"data": {"items": await _run_summaries(session, records, request.app.state.settings, moment),
        "total": total, "offset": offset, "limit": limit, "server_time": moment.isoformat()}}


@router.get("/recovery-runs/{run_id}")
async def recovery_run_detail(run_id: str, request: Request, user: Admin, session: Session,
        after: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
    run = await session.get(Run, run_id)
    if not run:
        raise AppError("NOT_FOUND", "任务不存在", 404)
    moment = await database_now(session)
    checkpoints = (await session.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id)
        .order_by(RunCheckpoint.created_at, RunCheckpoint.id))).all()
    budget = next((item.payload for item in checkpoints if item.checkpoint_key == "budget"), {})
    stored_calls = (await session.scalars(select(CallLog).where(CallLog.run_id == run_id)
        .order_by(CallLog.created_at, CallLog.id))).all()
    ledger = {}
    for number, call in enumerate(budget.get("calls", []), 1):
        if not isinstance(call, dict):
            continue
        call_id = str(call.get("call_id", f"call-{number}"))
        ledger[call_id] = {"id": f"budget:{run_id}:{call_id}", "created_at": None,
            "call_id": call_id, "model_name": call.get("model_name"),
            "provider": call.get("provider", call.get("llm_provider")), "status": call.get("call_outcome"),
            **{key: call.get(key) for key in ("call_outcome", "validation_outcome", "validation_codes", "execution_epoch")}}
    for call in stored_calls:
        metadata = call.metadata_json or {}
        call_id = metadata.get("call_id", call.id)
        ledger[call_id] = {"id": call.id, "created_at": call.created_at.isoformat(), "call_id": call_id,
            "model_name": call.model_name, "provider": call.provider, "status": call.status,
            **{key: metadata.get(key) for key in ("call_outcome", "validation_outcome", "validation_codes", "execution_epoch")}}
    events = (await session.scalars(select(Event).where(Event.run_id == run_id, Event.sequence > after)
        .order_by(Event.sequence).limit(limit))).all()
    alerts = (await session.scalars(select(RecoveryAlert).where(RecoveryAlert.run_id == run_id)
        .order_by(RecoveryAlert.last_seen_at.desc()))).all()
    settings = request.app.state.settings
    elapsed = max(0, ((run.finished_at or moment) - run.started_at).total_seconds()) if run.started_at else 0
    # Never load the potentially large private response body into an operational GET.
    columns = ("id", "invocation_key", "step_key", "attempt", "call_id", "call_index", "route", "status",
        "request_hash", "response_hash", "capture_epoch", "created_at", "response_saved_at", "applied_at", "error_code")
    receipts = (await session.execute(select(*(getattr(LLMInvocation, key) for key in columns),
        LLMInvocation.response_log).where(LLMInvocation.run_id == run_id)
        .order_by(LLMInvocation.created_at, LLMInvocation.id).limit(100))).mappings().all()
    from app.services.invocations import safe_response_log
    invocations = []
    for receipt in receipts:
        log = safe_response_log(receipt["response_log"])
        invocations.append({**{key: receipt[key].isoformat() if key.endswith("_at") and receipt[key] else receipt[key]
            for key in columns}, **{key: log.get(key) for key in
                ("provider_request_id", "model_name", "prompt_tokens", "completion_tokens", "total_tokens")}})
    return {"data": {
        "run": (await _run_summaries(session, [run], settings, moment))[0],
        "checkpoints": [{"id": item.id, "key": item.checkpoint_key, "created_at": item.created_at.isoformat(),
            "input_fingerprint": item.input_fingerprint, "content_hash": digest(item.payload.get("value"))} for item in checkpoints],
        "budget": {"calls_started": budget.get("calls_started", len(ledger)),
            "max_calls": request.app.state.runtime.assets.policy["max_calls"],
            "quality_retry_limit": settings.quality_retry_limit, "run_timeout_seconds": settings.run_timeout_seconds,
            "elapsed_seconds": int(elapsed), "attempt_counts": {key: value for key, value in budget.get("attempt_counts", {}).items()
                if isinstance(key, str) and len(key) <= 200 and isinstance(value, int) and value >= 0}},
        "calls": list(ledger.values()), "invocations": invocations, "event_total": run.sequence,
        "events": [{"sequence": item.sequence, "event_type": item.event_type,
            "created_at": item.created_at.isoformat(), "payload": _safe_metadata(item.payload)} for item in events],
        "alerts": [_alert_view(alert) for alert in alerts],
    }}


class RecoveryReason(BaseModel):
    reason: str = Field(min_length=4, max_length=300)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class RecoveryAction(RecoveryReason):
    expected_epoch: int = Field(ge=0, strict=True)


class RecoveryRetry(RecoveryAction):
    expected_attempts: int = Field(ge=0, strict=True)


async def _locked_action(session, run_id, expected_epoch):
    run, draft = await lock_run_with_draft(session, run_id)
    if not run:
        raise AppError("NOT_FOUND", "任务不存在", 404)
    if not await lock_matching_run(session, Run.id == run_id, Run.execution_epoch == expected_epoch):
        raise AppError("RECOVERY_STATE_CHANGED", "任务状态已变化，请刷新后操作", 409)
    await session.refresh(run)
    if run.execution_epoch != expected_epoch:
        raise AppError("RECOVERY_STATE_CHANGED", "任务执行批次已变化，请刷新后操作", 409)
    return run, draft


@router.post("/recovery-runs/{run_id}/retry")
async def recovery_retry(run_id: str, body: RecoveryRetry, request: Request, user: Admin, session: Session):
    run, _ = await _locked_action(session, run_id, body.expected_epoch)
    moment = await database_now(session)
    intent = await _current_intent(session, run, lock=True)
    if (intent.attempts if intent else 0) != body.expected_attempts:
        raise AppError("RECOVERY_STATE_CHANGED", "投递尝试次数已变化，请刷新后操作", 409)
    summary = (await _run_summaries(session, [run], request.app.state.settings, moment))[0]
    if not summary["can_retry"]:
        raise AppError("RECOVERY_NOT_ALLOWED", "当前任务不能重投；有效执行任务请等待，已结束任务请从教案页重新生成", 409)
    expired = run.status == "running"
    if not expired:
        intent.available_at = moment
    session.add(Audit(id=uid(), user_id=user.id, action="recovery.retry", resource_id=run_id,
        metadata_json={"reason": body.reason, "expected_epoch": body.expected_epoch,
                       "expected_attempts": body.expected_attempts, "operation": "recover_expired" if expired else "dispatch_now"}))
    await session.commit()
    dispatcher = request.app.state.dispatcher
    if expired and not await dispatcher.recover_run(run_id, expected_epoch=body.expected_epoch):
        raise AppError("RECOVERY_STATE_CHANGED", "任务已被其他执行者处理，请刷新后查看", 409)
    await dispatcher.submit(run_id, recover=True)
    session.expire_all()
    run = await session.get(Run, run_id)
    return {"data": (await _run_summaries(session, [run], request.app.state.settings, await database_now(session)))[0]}


@router.post("/recovery-runs/{run_id}/cancel")
async def recovery_cancel(run_id: str, body: RecoveryAction, request: Request, user: Admin, session: Session):
    from app.api.lessons import close_artifact
    from app.services.recovery import resolve_alerts

    run, draft = await _locked_action(session, run_id, body.expected_epoch)
    if run.status in TERMINAL:
        if run.status != "cancelled":
            raise AppError("RECOVERY_NOT_ALLOWED", "任务已结束，不能取消", 409)
    else:
        run.cancel_requested = True
        await finish(session, run, "cancelled", code="ADMIN_CANCELLED", message="管理员已取消任务")
        await close_artifact(session, run, "管理员已取消任务")
        if draft and run.workflow_type in {"one_click", "interactive_step"}:
            draft.status, draft.completion_status = "cancelled", "cancelled"
        await session.execute(update(Outbox).where(Outbox.run_id == run_id,
            Outbox.event_type == "dispatch_run", Outbox.status.in_(["pending", "dispatching", "checking", "published"]))
            .values(status="cancelled"))
        await resolve_alerts(session, run_id)
        session.add(Audit(id=uid(), user_id=user.id, action="recovery.cancel", resource_id=run_id,
            metadata_json={"reason": body.reason, "expected_epoch": body.expected_epoch}))
        await session.commit()
        await request.app.state.dispatcher.notify(run_id)
    return {"data": (await _run_summaries(session, [run], request.app.state.settings, await database_now(session)))[0]}


@router.get("/recovery-alerts")
async def recovery_alerts(user: Admin, session: Session, status: AlertStatus | None = None,
        offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=20, ge=1, le=100)):
    query = select(RecoveryAlert)
    if status:
        query = query.where(RecoveryAlert.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    alerts = (await session.scalars(query.order_by(RecoveryAlert.last_seen_at.desc(), RecoveryAlert.id.desc())
        .offset(offset).limit(limit))).all()
    return {"data": {"items": [_alert_view(alert) for alert in alerts], "total": total, "offset": offset, "limit": limit}}


async def _alert_action(session, alert_id, body, user, action):
    alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.id == alert_id).with_for_update())
    if not alert:
        raise AppError("NOT_FOUND", "告警不存在", 404)
    moment = await database_now(session)
    target = "acknowledged" if action == "acknowledge" else "resolved"
    eligible = ["open"] if action == "acknowledge" else ["open", "acknowledged"]
    values = ({"status": target, "acknowledged_at": moment, "acknowledged_by": user.id}
              if action == "acknowledge" else {"status": target, "resolved_at": moment})
    # A conditional transition also protects SQLite, whose FOR UPDATE is ignored.
    changed = await session.execute(update(RecoveryAlert).where(RecoveryAlert.id == alert_id,
        RecoveryAlert.status.in_(eligible)).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount:
        session.add(Audit(id=uid(), user_id=user.id, action=f"recovery.alert.{action}", resource_id=alert_id,
            metadata_json={"reason": body.reason, "code": alert.code}))
    await session.commit()
    await session.refresh(alert)
    return {"data": _alert_view(alert)}


@router.post("/recovery-alerts/{alert_id}/acknowledge")
async def acknowledge_recovery_alert(alert_id: str, body: RecoveryReason, user: Admin, session: Session):
    return await _alert_action(session, alert_id, body, user, "acknowledge")


@router.post("/recovery-alerts/{alert_id}/resolve")
async def resolve_recovery_alert(alert_id: str, body: RecoveryReason, user: Admin, session: Session):
    return await _alert_action(session, alert_id, body, user, "resolve")


ExecutionLevel = Literal["info", "warning", "error"]
ExecutionStatus = Literal["running", "completed", "failed", "cancelled", "lease_lost", "interrupted"]
ExecutionWorkflow = Literal["one_click", "interactive_step", "interactive_finalize", "module_polish_message",
    "reflection_polish_message", "export", "document_parse", "knowledge_document_parse", "lesson_internal_review"]


@router.get("/execution-logs")
async def execution_logs(request: Request, user: Admin, session: Session,
        run_id: str | None = Query(default=None, max_length=36),
        level: ExecutionLevel | None = None, status: ExecutionStatus | None = None,
        workflow: ExecutionWorkflow | None = None, after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100)):
    from app.db.models import ExecutionLog
    from app.services.execution_logs import execution_log_view, observability_status

    query = select(ExecutionLog, Run.workflow_type).join(Run, Run.id == ExecutionLog.run_id).where(
        ExecutionLog.sequence > after)
    if run_id:
        query = query.where(ExecutionLog.run_id == run_id)
    if level:
        query = query.where(ExecutionLog.level == level)
    if status:
        query = query.where(ExecutionLog.status == status)
    if workflow:
        query = query.where(Run.workflow_type == workflow)
    rows = (await session.execute(query.order_by(ExecutionLog.sequence).limit(limit + 1))).all()
    items = [execution_log_view(item, workflow_type) for item, workflow_type in rows[:limit]]
    return {"data": {"items": items, "next_after": items[-1]["sequence"] if items else after,
        "has_more": len(rows) > limit, "observability": observability_status(request.app.state)}}


@router.get("/observability")
async def observability(request: Request, user: Admin):
    from app.services.execution_logs import observability_status

    return {"data": observability_status(request.app.state)}
