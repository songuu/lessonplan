"""Persistent recovery alerts and safe structured diagnostics; callers own transactions."""

import json
import logging
import re
from types import SimpleNamespace

from sqlalchemy import case, or_, select, update

from app.db.models import RecoveryAlert, Run
from app.services.lessons import database_now, lock_matching_run, uid

logger = logging.getLogger(__name__)
MESSAGES = {
    "DISPATCH_RETRY": "任务暂未取得队列回执，系统将自动重试。",
    "DISPATCH_EXHAUSTED": "任务投递达到尝试次数或排队期限上限，请人工处理。",
    "WORKER_LEASE_EXPIRED": "执行进程租约已过期，系统正在恢复任务。",
    "WORKER_RECOVERY_EXHAUSTED": "任务自动恢复次数已耗尽，请人工处理。",
    "QUEUE_BACKLOG": "任务排队时间超过预期，请检查队列与执行进程。",
    "LLM_OUTCOME_UNKNOWN": "模型调用结果尚未确认，已停止自动重发以避免重复计费，请核对服务商记录。",
}
TRANSIENT_CODES = ("DISPATCH_RETRY", "WORKER_LEASE_EXPIRED", "QUEUE_BACKLOG")
ALERT_SCAN_BATCH = 100


def safe_metadata(metadata):
    result = {}
    for key, value in (metadata or {}).items():
        if key in {"execution_epoch", "recovery_count", "attempts", "max_attempts", "age_seconds"}:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12:
                result[key] = value
        elif key in {"queue_name", "phase", "last_error_code", "status"}:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", value):
                result[key] = value
    return result


def recovery_log(event_name, *, code=None, run_id=None, status=None, metadata=None):
    payload = {"event": event_name, "code": code, "run_id": run_id, "status": status,
               "metadata": safe_metadata(metadata)}
    logger.warning(json.dumps(payload, ensure_ascii=False, sort_keys=True))


async def signal_alert(session, code, message, run=None, severity="warning", metadata=None, dedupe_key=None):
    if code not in MESSAGES or severity not in {"info", "warning", "error", "critical"}:
        raise ValueError("Unsupported recovery alert code or severity")
    run_id = run.id if run is not None else None
    if run_id is not None:
        await lock_matching_run(session, Run.id == run_id)
    moment = await database_now(session)
    values = {"id": uid(), "dedupe_key": dedupe_key or f"{code}:{run_id or 'global'}",
        "run_id": run_id, "code": code, "message": MESSAGES[code], "severity": severity,
        "status": "open", "occurrence_count": 1, "created_at": moment,
        "first_seen_at": moment, "last_seen_at": moment, "metadata_json": safe_metadata(metadata)}
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(RecoveryAlert).values(**values)
    reopened = RecoveryAlert.status == "resolved"
    statement = statement.on_conflict_do_update(index_elements=[RecoveryAlert.dedupe_key], set_={
        "last_seen_at": moment, "occurrence_count": RecoveryAlert.occurrence_count + 1,
        "message": values["message"], "severity": severity, "metadata_json": values["metadata_json"],
        "status": case((reopened, "open"), else_=RecoveryAlert.status),
        "acknowledged_at": case((reopened, None), else_=RecoveryAlert.acknowledged_at),
        "acknowledged_by": case((reopened, None), else_=RecoveryAlert.acknowledged_by),
        "resolved_at": case((reopened, None), else_=RecoveryAlert.resolved_at),
    }).returning(RecoveryAlert).execution_options(populate_existing=True)
    alert = await session.scalar(statement)
    recovery_log("recovery_alert_signalled", code=code, run_id=run_id, status=alert.status,
                 metadata={**values["metadata_json"], "attempts": (metadata or {}).get("attempts", 0)})
    return alert


async def resolve_alerts(session, run_id, codes=None):
    selected = list(MESSAGES) if codes is None else [code for code in codes if code in MESSAGES]
    if not selected:
        return 0
    await lock_matching_run(session, Run.id == run_id)
    moment = await database_now(session)
    result = await session.execute(update(RecoveryAlert).where(RecoveryAlert.run_id == run_id,
        RecoveryAlert.code.in_(selected), RecoveryAlert.status != "resolved")
        .values(status="resolved", resolved_at=moment).execution_options(synchronize_session=False))
    if result.rowcount:
        recovery_log("recovery_alert_resolved", run_id=run_id, status="resolved")
    return result.rowcount


async def scan_alerts(app):
    # A cursor bounds each pass while UUID ordering eventually visits newly inserted rows.
    # Select only operational columns: scanning must never materialize teacher/provider bodies.
    async with app.state.db.session_factory() as session:
        unresolved = select(RecoveryAlert.run_id).where(RecoveryAlert.code.in_(TRANSIENT_CODES),
            RecoveryAlert.status != "resolved", RecoveryAlert.run_id.is_not(None))
        query = select(Run.id).where(or_(Run.status.in_(["queued", "running"]), Run.id.in_(unresolved)))
        cursor = getattr(app.state, "recovery_scan_cursor", None)
        page = (await session.scalars(query.where(Run.id > cursor).order_by(Run.id).limit(ALERT_SCAN_BATCH)
            if cursor else query.order_by(Run.id).limit(ALERT_SCAN_BATCH))).all()
        if not page and cursor:
            page = (await session.scalars(query.order_by(Run.id).limit(ALERT_SCAN_BATCH))).all()
        for run_id in page:
            # Recheck after taking the same Run lock used by execution/dispatch/alert writers.
            # A worker may have completed between selecting the page and processing this row.
            if not await lock_matching_run(session, Run.id == run_id):
                continue
            row = (await session.execute(select(Run.id, Run.status, Run.cancel_requested,
                Run.created_at, Run.lease_expires_at, Run.execution_epoch, Run.recovery_count,
                Run.queue_name).where(Run.id == run_id))).one()
            run = SimpleNamespace(**row._mapping)
            moment = await database_now(session)
            if run.cancel_requested or run.status not in {"queued", "running"}:
                await resolve_alerts(session, run.id, TRANSIENT_CODES)
                continue
            if run.status == "queued":
                age = max(0, int((moment - run.created_at).total_seconds()))
                if age >= app.state.settings.queue_backlog_seconds:
                    await signal_alert(session, "QUEUE_BACKLOG", MESSAGES["QUEUE_BACKLOG"], run,
                        metadata={"age_seconds": age, "queue_name": run.queue_name or ""})
                else:
                    await resolve_alerts(session, run.id, ["QUEUE_BACKLOG"])
            else:
                await resolve_alerts(session, run.id, ["DISPATCH_RETRY", "QUEUE_BACKLOG"])
                if run.lease_expires_at is not None and run.lease_expires_at > moment:
                    await resolve_alerts(session, run.id, ["WORKER_LEASE_EXPIRED"])
                else:
                    await signal_alert(session, "WORKER_LEASE_EXPIRED", MESSAGES["WORKER_LEASE_EXPIRED"], run,
                        metadata={"execution_epoch": run.execution_epoch, "recovery_count": run.recovery_count})
        await session.commit()
        app.state.recovery_scan_cursor = page[-1] if page else None
