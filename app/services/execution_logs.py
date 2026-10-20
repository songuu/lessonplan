"""Bounded local diagnostics: private text and business transactions never enter this writer."""

import asyncio
import logging
import math
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Draft, ExecutionLog, Run

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_.:-]{0,119}\Z")
_SENSITIVE = re.compile(r"sk-|lsv2_|bearer|private|secret|password|api[_-]?key", re.IGNORECASE)
_INTEGER_METADATA = {
    "execution_epoch", "capture_epoch", "attempt", "attempt_index", "attempt_number", "quality_attempt",
    "call_index", "calls_started", "max_calls", "prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens", "first_token_ms", "elapsed_ms", "retry_count", "recovery_count",
    "chunk_count", "document_count", "module_count", "activity_count", "result_count", "selected_count",
    "source_count", "context_chars", "checkpoint_count", "validation_count", "count", "status_code",
}
_BOOLEAN_METADATA = {"usage_known", "usage_reported", "is_demo", "fallback", "sampled", "replayed", "truncated",
    "receipt_reused", "receipt_replayed"}
_TOKEN_METADATA = {
    "business_run_id", "source_business_run_id", "thread_id", "previous_trace_id", "origin_trace_id",
    "execution_trace_id", "llm_span_id", "invocation_id", "call_id", "step_key", "checkpoint_key",
    "route", "workflow", "workflow_type", "module_id", "module_type", "stage", "phase", "gate",
    "attempt_role", "provider", "llm_provider", "ls_provider", "model_name", "ls_model_name",
    "prompt_version", "policy_version", "release", "deployment_id", "trace_schema_version",
    "transport_outcome", "call_outcome", "validation_outcome", "finish_reason", "cost_status", "error_code",
    "code", "reason_code", "response_hash", "request_hash", "asset_fingerprint", "context_fingerprint",
    "input_fingerprint", "content_hash", "provider_request_id", "event_name", "queue_name",
    "document_id", "knowledge_base_id", "reference_id", "source_id", "status", "result_status",
}
_CODE_LIST_METADATA = {"validation_codes", "finding_codes", "module_ids"}


def safe_token(value, *, max_length=200):
    """Identifiers may cross the diagnostics boundary; arbitrary free text may not."""
    if (not isinstance(value, str) or len(value) > max_length or not _TOKEN.fullmatch(value)
            or _SENSITIVE.search(value)):
        return None
    return value


def safe_metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _INTEGER_METADATA:
        item = value.get(key)
        if type(item) is int and 0 <= item <= 2**63 - 1:
            result[key] = item
        elif key in {"first_token_ms", "elapsed_ms"} and type(item) is float and math.isfinite(item) and 0 <= item <= 2**31 - 1:
            result[key] = int(item)
    for key in _BOOLEAN_METADATA:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in _TOKEN_METADATA:
        if (item := safe_token(value.get(key))) is not None:
            result[key] = item
    for key in _CODE_LIST_METADATA:
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [item for raw in items[:32] if (item := safe_token(raw, max_length=100)) is not None]
    return result


def _uuid(value):
    return str(UUID(value)) if isinstance(value, str) and len(value) <= 36 else None


def _timestamp(value):
    if isinstance(value, str) and len(value) <= 40:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("invalid diagnostic timestamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sanitize(payload):
    if not isinstance(payload, dict):
        return None
    ids = {key: _uuid(payload.get(key)) for key in ("event_id", "trace_id", "span_id")}
    run_id = _uuid(payload.get("business_run_id"))
    parent = _uuid(payload["parent_span_id"]) if payload.get("parent_span_id") is not None else None
    phase, level, status = (payload.get(key) for key in ("phase", "level", "status"))
    name, kind = payload.get("name"), payload.get("run_type")
    epoch, elapsed = payload.get("execution_epoch", 0), payload.get("elapsed_ms")
    if type(elapsed) is float and math.isfinite(elapsed) and 0 <= elapsed <= 2**31 - 1:
        elapsed = int(elapsed)
    if (not all(ids.values()) or not run_id or (payload.get("parent_span_id") and not parent)
            or phase not in {"start", "end", "event"} or level not in {"info", "warning", "error"}
            or status not in {"running", "completed", "failed", "cancelled", "lease_lost", "interrupted"}
            or not isinstance(name, str) or not _NAME.fullmatch(name)
            or kind not in {"chain", "llm", "tool", "retriever", "parser", "prompt", "embedding"}
            or type(epoch) is not int or not 0 <= epoch <= 2**31 - 1
            or (elapsed is not None and (type(elapsed) is not int or not 0 <= elapsed <= 2**31 - 1))):
        return None
    return {**ids, "run_id": run_id, "parent_span_id": parent, "execution_epoch": epoch,
        "phase": phase, "name": name, "run_type": kind, "level": level, "status": status,
        "started_at": _timestamp(payload.get("started_at")),
        "ended_at": _timestamp(payload["ended_at"]) if payload.get("ended_at") else None,
        "elapsed_ms": elapsed, "metadata_json": safe_metadata(payload.get("metadata"))}


async def _serialize_sequence(session):
    if session.get_bind().dialect.name == "postgresql":
        # Cursor order must match commit order across independent Worker processes.
        await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
        await session.execute(text("SELECT pg_advisory_xact_lock(721946120061)"))


class ExecutionLogWriter:
    def __init__(self, session_factory, *, max_queue=512):
        self._session_factory = session_factory
        self._queue = asyncio.Queue(maxsize=max(1, max_queue))
        self._task = None
        self._closed = False
        self._accepting = True
        self._written = self._dropped = self._errors = self._duplicates = self._skipped = 0
        self._flush_timeouts = 0

    def submit(self, payload):
        # Sanitizing and copying before admission avoids queuing mutable or private source objects.
        try:
            item = _sanitize(payload)
            if item is None or not self._accepting:
                self._dropped += 1
                return
            self._queue.put_nowait(item)
        except Exception:
            self._dropped += 1

    async def start(self):
        if not self._closed and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._consume(), name="execution-log-writer")

    async def flush(self, timeout=2):
        if not self._closed and self._task is None and not self._queue.empty():
            await self.start()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=max(0.001, timeout))
            return True
        except TimeoutError:
            self._flush_timeouts += 1
            return False

    async def close(self, timeout=5):
        if self._closed:
            return
        self._accepting = False
        await self.flush(timeout=timeout)
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=0.1)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped += 1

    def status(self):
        return {"enabled": True, "running": self._task is not None and not self._task.done() and not self._closed,
            "queued": self._queue.qsize(), "capacity": self._queue.maxsize, "written": self._written,
            "dropped": self._dropped, "errors": self._errors, "duplicates": self._duplicates,
            "skipped": self._skipped, "flush_timeouts": self._flush_timeouts}

    async def _consume(self):
        while True:
            batch = [await self._queue.get()]
            while len(batch) < 64 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._store(batch)
            except asyncio.CancelledError:
                self._dropped += len(batch)
                raise
            except Exception:
                # Database exception text may contain connection credentials or SQL parameters.
                self._errors += 1
                self._dropped += len(batch)
                logger.warning("execution_log_write_failed")
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _store(self, batch):
        by_id = {item["event_id"]: item for item in batch}
        self._duplicates += len(batch) - len(by_id)
        async with self._session_factory() as session:
            await _serialize_sequence(session)
            # Diagnostics can describe an old epoch, but deleted teaching material is excluded.
            allowed = set(await session.scalars(select(Run.id).outerjoin(Draft, Draft.id == Run.draft_id)
                .where(Run.id.in_({item["run_id"] for item in by_id.values()}),
                    or_(Run.draft_id.is_(None), Draft.deleted_at.is_(None)))))
            existing = set(await session.scalars(select(ExecutionLog.event_id).where(
                ExecutionLog.event_id.in_(by_id))))
            self._duplicates += len(existing)
            records = [item for key, item in by_id.items() if key not in existing and item["run_id"] in allowed]
            self._skipped += sum(item["run_id"] not in allowed for key, item in by_id.items() if key not in existing)
            session.add_all(ExecutionLog(**item) for item in records)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # Concurrent exporters can repeat an event ID. Preserve other rows in that batch.
                for item in records:
                    async with self._session_factory() as retry:
                        await _serialize_sequence(retry)
                        if await retry.scalar(select(ExecutionLog.sequence).where(ExecutionLog.event_id == item["event_id"])):
                            self._duplicates += 1
                            continue
                        try:
                            retry.add(ExecutionLog(**item))
                            await retry.commit()
                        except IntegrityError:
                            await retry.rollback()
                            self._skipped += 1
                        else:
                            self._written += 1
            else:
                self._written += len(records)


_OBS_BOOLEAN_KEYS = {"enabled", "configured", "sampled", "running", "accepting", "circuit_open",
    "remote_enabled", "local_enabled", "shutdown_timed_out"}
_OBS_COUNT_KEYS = {"queued", "queue_depth", "queue_size", "capacity", "written", "dropped", "errors", "duplicates",
    "skipped", "flush_timeouts", "sent", "exported", "failed", "export_errors", "consecutive_failures", "active_spans",
    "pending", "queue_capacity", "local_dropped", "payload_dropped"}
_OBS_TEXT_KEYS = {"provider", "project", "region", "capture_mode", "release", "last_error_code", "status", "disabled_reason"}


def safe_status(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _OBS_BOOLEAN_KEYS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in _OBS_COUNT_KEYS:
        if type(value.get(key)) is int and 0 <= value[key] <= 2**63 - 1:
            result[key] = value[key]
    for key in _OBS_TEXT_KEYS:
        if (item := safe_token(value.get(key))) is not None:
            result[key] = item
    sample_rate = value.get("sample_rate")
    if type(sample_rate) in {int, float} and math.isfinite(sample_rate) and 0 <= sample_rate <= 1:
        result["sample_rate"] = sample_rate
    return result


def observability_status(state):
    result = {"provider": "langfuse", "enabled": False, "configured": False, "local": {"enabled": False}}
    for attribute in ("observer", "execution_logs"):
        component = getattr(state, attribute, None)
        try:
            status = safe_status(component.status()) if component is not None else {}
        except Exception:
            status = {"status": "unavailable"}
        if attribute == "observer":
            result.update(status)
        elif status:
            result["local"] = status
    return result


def execution_log_view(item, workflow_type):
    return {"sequence": item.sequence, "event_id": item.event_id, "run_id": item.run_id,
        "execution_epoch": item.execution_epoch, "trace_id": item.trace_id, "span_id": item.span_id,
        "parent_span_id": item.parent_span_id, "phase": item.phase, "name": item.name,
        "run_type": item.run_type, "level": item.level, "status": item.status,
        "started_at": item.started_at.isoformat(), "ended_at": item.ended_at.isoformat() if item.ended_at else None,
        "elapsed_ms": item.elapsed_ms, "metadata": safe_metadata(item.metadata_json),
        "created_at": item.created_at.isoformat(), "workflow_type": workflow_type}
