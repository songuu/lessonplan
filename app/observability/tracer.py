"""Metadata-only local spans with optional bounded, redacted remote content."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.observability.context import (
    current_span,
    execution_trace_id,
    lesson_thread_id,
    observer_context,
    span_context,
)

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/+\-]{0,127}\Z")
_SECRET = re.compile(r"(?:sk-[a-zA-Z0-9_\-]{8,}|pk-lf-|lsv2_|bearer|password|secret|api[_\-]?key)", re.IGNORECASE)
_STRING_FIELDS = frozenset({
    "workflow", "workflow_type", "route", "model", "ls_provider", "ls_model_name", "provider",
    "module_id", "module_type", "stage", "step_key", "call_id", "invocation_id", "attempt_role",
    "code", "error_code", "transport_outcome", "validation_outcome", "release", "outcome",
    "business_outcome", "finish_reason", "cost_status", "event_name", "thread_id", "previous_trace_id",
    "origin_trace_id", "execution_trace_id", "llm_span_id", "source_business_run_id", "response_hash",
    "asset_fingerprint", "prompt_fingerprint", "prompt_version", "schema_version", "retrieval_method",
    "method", "checkpoint_kind", "reason_code", "result_code", "action", "operation", "queue",
    "queue_role", "revision_id", "version_id", "model_price_version", "observability_error_code",
    "model_name", "llm_provider", "checkpoint_key", "gate", "policy_version", "deployment_id",
    "trace_schema_version", "call_outcome", "request_hash", "context_fingerprint", "input_fingerprint",
    "content_hash", "provider_request_id", "queue_name", "result_status", "document_id", "knowledge_base_id",
})
_BOOL_FIELDS = frozenset({
    "receipt_reused", "usage_known", "usage_reported", "is_demo", "resumed", "sampled", "streaming",
    "fallback_used", "checkpoint_reused", "metadata_truncated", "committed", "cached", "enabled",
    "input_truncated", "output_truncated", "input_capture_failed", "output_capture_failed",
})
_NUMBER_FIELDS = frozenset({
    "quality_attempt", "attempt", "transport_attempt", "capture_epoch", "execution_epoch",
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens",
    "first_token_ms", "elapsed_ms", "sample_rate", "sampling_rate", "module_count", "activity_count",
    "candidate_count", "hit_count", "frozen_count", "chunk_count", "document_count", "item_count",
    "retry_count", "max_calls", "max_tokens", "response_bytes", "content_length", "count", "result_count",
    "selected_count", "source_count", "context_chars", "checkpoint_count", "validation_count", "calls_started",
    "call_index", "attempt_index", "attempt_number", "recovery_count", "status_code",
})
_TOKEN_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"})
_FRACTION_FIELDS = frozenset({"first_token_ms", "elapsed_ms", "sample_rate", "sampling_rate"})
_IDENTITY_FIELDS = frozenset({"thread_id", "previous_trace_id", "sampled", "sample_rate", "schema_version"})
_SAFE_FIELDS = _STRING_FIELDS | _BOOL_FIELDS | _NUMBER_FIELDS
_STATUSES = frozenset({"running", "completed", "failed", "cancelled", "lease_lost", "interrupted"})
_RUN_TYPES = frozenset({"chain", "llm", "tool", "retriever", "prompt", "parser", "embedding"})


class FrozenDict(dict):
    """JSON-compatible snapshot that callbacks cannot mutate accidentally."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("observability snapshots are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def safe_token(value: Any, *, fallback: str | None = None) -> str | None:
    # Never stringify arbitrary objects: __str__ may expose credentials or execute business code.
    if not isinstance(value, str) or not _TOKEN.fullmatch(value) or _SECRET.search(value):
        return fallback
    return value


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(metadata, Mapping):
        return result
    for key in _SAFE_FIELDS:
        try:
            value = metadata.get(key)
        except Exception:
            continue
        if key in _STRING_FIELDS:
            token = safe_token(value)
            if token is not None:
                result[key] = token
        elif key in _BOOL_FIELDS and isinstance(value, bool):
            result[key] = value
        elif key in _NUMBER_FIELDS:
            if type(value) is int and 0 <= value <= 10**15:
                result[key] = value
            elif key in _FRACTION_FIELDS and type(value) is float and math.isfinite(value) and 0 <= value <= 10**15:
                result[key] = value
    return result


def content_limit(settings: Any) -> int:
    return max(1024, min(1048576, int(getattr(settings, "obs_max_content_bytes", 65536))))


def export_event_budget(settings: Any) -> int:
    budget = max(1024, int(getattr(settings, "obs_max_event_bytes", 16384)))
    if getattr(settings, "obs_capture_mode", "metadata") == "full":
        budget += 2 * content_limit(settings) + 1024
    return budget


@dataclass
class _TraceState:
    sampled: bool
    business_run_id: str | None
    epoch: int | None
    metadata: dict[str, Any]
    ordinary_count: int = 0
    request_count: int = 0
    limit_announced: bool = False
    root: Span | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(self, run_type: str, max_spans: int) -> bool:
        # Current workflow policy permits 32 provider requests. Reserve those slots before detail spans.
        with self.lock:
            if self.ordinary_count + self.request_count >= max_spans:
                return False
            if run_type != "llm" and self.ordinary_count >= max(1, max_spans - 32):
                return False
            if run_type == "llm":
                self.request_count += 1
            else:
                self.ordinary_count += 1
            return True


class Span:
    def __init__(self, observer: Observer, name: str, run_type: str, state: _TraceState,
                 parent: Span | None, metadata: Mapping[str, Any] | None, span_id: str | None = None):
        self.observer = observer
        self.name = safe_token(name, fallback="operation")
        self.run_type = run_type if run_type in _RUN_TYPES else "chain"
        self.state = state
        self.span_id = _valid_uuid(span_id) or str(uuid4())
        self.trace_id = parent.trace_id if parent else self.span_id
        self.parent_span_id = parent.span_id if parent and parent.recorded else (parent.parent_span_id if parent else None)
        self.started_at = datetime.now(UTC)
        self._started_monotonic = time.monotonic()
        self._metadata = dict(parent._metadata if parent else state.metadata)
        # Usage belongs to a physical request, never its logical parent or a receipt replay.
        for key in _TOKEN_FIELDS | {"usage_known", "usage_reported", "first_token_ms"}:
            self._metadata.pop(key, None)
        self._metadata.update(sanitize_metadata(metadata))
        self._metadata.update(state.metadata)
        if self.run_type != "llm" or self._metadata.get("receipt_reused"):
            for key in _TOKEN_FIELDS | {"usage_known", "usage_reported"}:
                self._metadata.pop(key, None)
        self.status = "running"
        self._events: list[dict[str, Any]] = []
        self._content: dict[str, Any] = {}
        self._ended = False
        self.recorded = state.admit(self.run_type, observer.max_spans)

    @property
    def metadata(self) -> dict[str, Any]:
        return FrozenDict(self._metadata)

    @property
    def capture_enabled(self) -> bool:
        return (not self._ended and self.recorded and self.observer.capture_full
                and self.state.sampled and self.observer.cloud_requested)

    def set(self, **metadata: Any) -> None:
        if self._ended:
            return
        status = metadata.pop("status", None)
        if isinstance(status, str) and status in _STATUSES:
            self.status = status
        safe = sanitize_metadata(metadata)
        for key in _IDENTITY_FIELDS:
            if key in self.state.metadata:
                safe.pop(key, None)
        if self.run_type != "llm" or self._metadata.get("receipt_reused") or safe.get("receipt_reused"):
            for key in _TOKEN_FIELDS | {"usage_known", "usage_reported"}:
                safe.pop(key, None)
                self._metadata.pop(key, None)
        self._metadata.update(safe)

    def fail(self, code: str) -> None:
        code = safe_token(code, fallback="operation_failed")
        status = "cancelled" if code in {"cancelled", "run_cancelled"} else "failed"
        if code in {"lease_lost", "execution_lease_lost", "run_lease_lost"}:
            status = "lease_lost"
        self.set(status=status, error_code=code)

    def set_input(self, value: Any) -> None:
        self._capture("input", value)

    def set_output(self, value: Any) -> None:
        self._capture("output", value)

    def _capture(self, name: str, value: Any) -> None:
        if not self.capture_enabled:
            return
        from app.observability.content import sanitize_content

        try:
            captured, truncated = sanitize_content(value, max_bytes=self.observer.max_content_bytes,
                                                   secrets=self.observer.content_secrets)
            self._content[name] = captured
            self.set(**{f"{name}_truncated": truncated})
        except Exception:
            self.set(**{f"{name}_capture_failed": True})

    def event(self, name: str) -> None:
        name = safe_token(name)
        if self._ended or not name or len(self._events) >= 16:
            return
        if name == "new_token" and any(event["name"] == name for event in self._events):
            return
        self._events.append({"name": name, "time": datetime.now(UTC).isoformat()})
        if name == "new_token" and self.run_type == "llm":
            self.set(first_token_ms=round((time.monotonic() - self._started_monotonic) * 1000, 3))
        self.observer._emit(self, "event", event_name=name)

    def usage(self, usage_dict: Mapping[str, Any]) -> None:
        if self.run_type != "llm" or self._metadata.get("receipt_reused") or self._ended:
            return
        if not isinstance(usage_dict, Mapping):
            usage_dict = {}
        explicit = usage_dict.get("usage_reported", usage_dict.get("usage_known"))
        values: dict[str, int] = {}
        if explicit is not False:
            for output_key, legacy_key in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                                           ("total_tokens", "total_tokens")):
                value = usage_dict.get(output_key, usage_dict.get(legacy_key))
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**15:
                    values[output_key] = value
        if "total_tokens" not in values and "input_tokens" in values and "output_tokens" in values:
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        for key in _TOKEN_FIELDS:
            self._metadata.pop(key, None)
        self.set(**values, usage_known=bool(values), usage_reported=bool(values),
                 cost_status="demo" if self._metadata.get("is_demo") else ("unpriced" if values else "unknown"))

    def _finish(self, error: BaseException | None) -> None:
        if self._ended:
            return
        if error is not None:
            if isinstance(error, asyncio.CancelledError):
                self.fail("cancelled")
            elif type(error).__name__ in {"LeaseLost", "ExecutionLeaseLost"}:
                self.fail("lease_lost")
            elif isinstance(error, TimeoutError):
                self.fail("timeout")
            elif self.status == "running":
                self.fail("operation_failed")
        if self.status == "running":
            self.status = "completed"
        self._ended = True
        try:
            self.observer._emit(self, "end")
        finally:
            self._content.clear()


def _valid_uuid(value: Any) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


class Observer:
    def __init__(self, settings: Any, *, local_sink: Callable[[dict], Any] | None = None, exporter=None):
        self.settings = settings
        self.local_sink = local_sink
        self.exporter = exporter
        self.cloud_requested = bool(getattr(settings, "langfuse_enabled", False))
        self.deployment_id = safe_token(getattr(settings, "obs_deployment_id", "local"), fallback="local")
        self.sample_rate = min(1.0, max(0.0, float(getattr(settings, "obs_trace_sample_rate", 1.0))))
        self.max_event_bytes = max(1024, int(getattr(settings, "obs_max_event_bytes", 16384)))
        self.max_content_bytes = content_limit(settings)
        self.capture_full = getattr(settings, "obs_capture_mode", "metadata") == "full"
        self.content_secrets: tuple[str, ...] = ()
        if self.capture_full and self.cloud_requested:
            from app.observability.content import configured_secrets

            self.content_secrets = configured_secrets(settings)
        self.max_spans = max(33, min(256, int(getattr(settings, "obs_max_spans_per_trace", 256))))
        self.local_dropped = 0
        self.payload_dropped = 0
        self.closed = False

    def trace_id_for(self, run_id: str, epoch: int) -> str:
        return execution_trace_id(self.deployment_id, str(run_id), int(epoch))

    def thread_id_for(self, run_id: str, draft_id: str | None = None, tenant_id: str | None = None) -> str:
        return lesson_thread_id(self.deployment_id, str(run_id), draft_id, tenant_id)

    def _sampled(self, run_id: str) -> bool:
        digest = hashlib.sha256(f"{self.deployment_id}:{run_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64 < self.sample_rate

    @contextmanager
    def execution(self, run_id: str, epoch: int, *, draft_id: str | None = None,
                  tenant_id: str | None = None, workflow: str | None = None) -> Iterator[Span]:
        metadata = sanitize_metadata({"workflow": workflow, "release": getattr(self.settings, "obs_release", None)})
        metadata.update(thread_id=self.thread_id_for(run_id, draft_id, tenant_id), resumed=epoch > 0,
                        sampled=self._sampled(str(run_id)), sample_rate=self.sample_rate, schema_version="1")
        if epoch > 0:
            metadata["previous_trace_id"] = self.trace_id_for(run_id, epoch - 1)
        state = _TraceState(metadata["sampled"], safe_token(str(run_id)), epoch, metadata)
        span = Span(self, "lesson.execute", "chain", state, None, None, self.trace_id_for(run_id, epoch))
        state.root = span
        with self._activate(span):
            yield span

    @contextmanager
    def span(self, name: str, *, run_type: str = "chain", metadata: Mapping[str, Any] | None = None,
             span_id: str | None = None) -> Iterator[Span]:
        parent = current_span()
        if parent and (parent.observer is not self or parent._ended):
            parent = None
        if parent:
            state = parent.state
        else:
            identity = _valid_uuid(span_id) or str(uuid4())
            base = {"sampled": self._sampled(identity), "sample_rate": self.sample_rate, "schema_version": "1"}
            state = _TraceState(base["sampled"], None, None, base)
        span = Span(self, name, run_type, state, parent, metadata, span_id)
        if state.root is None:
            state.root = span
        if not span.recorded and not state.limit_announced:
            state.limit_announced = True
            state.root.event("span_limit_reached")
        with self._activate(span):
            yield span

    @contextmanager
    def _activate(self, span: Span) -> Iterator[Span]:
        observer_token = observer_context.set(self)
        span_token = span_context.set(span)
        self._emit(span, "start")
        error = None
        try:
            yield span
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                span._finish(error)
            finally:
                span_context.reset(span_token)
                observer_context.reset(observer_token)

    def _emit(self, span: Span, phase: str, *, event_name: str | None = None) -> None:
        if not span.recorded or self.closed:
            return
        metadata = dict(span._metadata)
        if event_name:
            metadata["event_name"] = event_name
        elapsed_ms = round(max(0, time.monotonic() - span._started_monotonic) * 1000)
        event = {
            "event_id": str(uuid4()), "phase": phase, "span_id": span.span_id, "trace_id": span.trace_id,
            "parent_span_id": span.parent_span_id, "business_run_id": span.state.business_run_id,
            "execution_epoch": span.state.epoch, "name": span.name, "run_type": span.run_type,
            "started_at": span.started_at.isoformat(), "ended_at": datetime.now(UTC).isoformat() if phase == "end" else None,
            "elapsed_ms": elapsed_ms, "level": "error" if span.status == "failed" else (
                "warning" if span.status in {"cancelled", "lease_lost", "interrupted"} else "info"),
            "status": span.status, "metadata": metadata,
            "trace_name": span.state.root.name if span.state.root else span.name,
            "events": list(span._events) if phase == "end" else [],
        }
        # Preserve identity, then remove optional detail deterministically until the snapshot fits.
        while len(json.dumps(event, ensure_ascii=True).encode()) > self.max_event_bytes:
            if event["events"]:
                event["events"].pop()
            elif metadata:
                metadata.pop(next(reversed(metadata)))
            else:
                self.payload_dropped += 1
                return
        snapshot = _freeze(event)
        if self.local_sink is not None:
            try:
                result = self.local_sink(snapshot)
                # A callback must enqueue synchronously; accidentally returned coroutines cannot be leaked.
                if inspect.iscoroutine(result):
                    result.close()
                    self.local_dropped += 1
            except Exception:
                self.local_dropped += 1
        if self.cloud_requested and span.state.sampled and self.exporter is not None:
            try:
                # Content belongs only to this completed observation, never local logs or child metadata.
                remote = dict(event)
                if phase == "end" and self.capture_full:
                    remote.update(span._content)
                self.exporter.submit(_freeze(remote))
            except Exception:
                # Instrumentation must never replace or swallow a business exception.
                self.payload_dropped += 1

    def start(self) -> None:
        if self.exporter is not None and self.cloud_requested and not self.closed:
            try:
                self.exporter.start()
            except Exception:
                self.payload_dropped += 1

    def close(self, timeout: float | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        if self.exporter is not None:
            try:
                self.exporter.close(timeout=timeout)
            except Exception:
                self.payload_dropped += 1

    def status(self) -> dict[str, Any]:
        result = {"platform": "langfuse", "provider": "langfuse", "enabled": False, "configured": False,
                  "remote_enabled": False, "local_enabled": self.local_sink is not None,
                  "closed": self.closed, "sample_rate": self.sample_rate, "local_dropped": self.local_dropped,
                  "payload_dropped": self.payload_dropped, "disabled_reason": "observability_disabled"}
        if self.exporter is not None:
            try:
                result.update(self.exporter.status())
            except Exception:
                result["disabled_reason"] = "observability_status_failed"
        return result
