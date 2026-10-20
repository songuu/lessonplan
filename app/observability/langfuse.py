"""Bounded Langfuse OTLP export, independent from business execution and global OTel state.

The high-level Langfuse client installs additional batch/media/score queues. This adapter
uses its versioned semantic attributes and the OpenTelemetry SDK encoder directly, so
the application owns the only queue, timeout and retry policy. No legacy ingestion API.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from app.observability.tracer import (
    FrozenDict,
    content_limit,
    export_event_budget,
    safe_token,
    sanitize_metadata,
)

logger = logging.getLogger(__name__)


def _plain_snapshot(value: Any) -> Any:
    # Only our own frozen snapshots need thawing; unknown user objects stay opaque to the sanitizer.
    if type(value) is FrozenDict:
        return {key: _plain_snapshot(child) for key, child in value.items()}
    if type(value) is tuple:
        return [_plain_snapshot(child) for child in value]
    return value


def _secret(value: Any) -> str:
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return value if isinstance(value, str) else ""


def _configuration_error(settings: Any) -> str | None:
    if not getattr(settings, "langfuse_enabled", False):
        return "observability_disabled"
    if getattr(settings, "obs_capture_mode", "metadata") not in {"metadata", "full"}:
        return "observability_capture_mode_unsupported"
    if not (_secret(getattr(settings, "langfuse_public_key", ""))
            and _secret(getattr(settings, "langfuse_secret_key", ""))):
        return "observability_credentials_missing"
    endpoint = getattr(settings, "langfuse_base_url", "")
    if not isinstance(endpoint, str) or not endpoint:
        return "observability_endpoint_missing"
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return "observability_endpoint_invalid"
    except ValueError:
        return "observability_endpoint_invalid"
    return None


class ExportError(Exception):
    """Only controlled diagnostics cross the monitoring transport boundary."""

    def __init__(self, code: str, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def trace_hex(trace_id: str) -> str:
    return UUID(str(trace_id)).hex


def span_hex(span_id: str) -> str:
    # OTLP uses 128-bit traces and 64-bit observations; local UUID identities stay unchanged.
    return f"{(UUID(str(span_id)).int & ((1 << 64) - 1)) or 1:016x}"


def _nanoseconds(timestamp: str) -> int:
    delta = datetime.fromisoformat(timestamp).astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


class SDKTransport:
    """Langfuse semantic attributes + OTel SDK serialization + one synchronous HTTP send.

    Instantiate only in the application's sender thread. Explicit HTTPX MockTransport
    injection permits offline contract tests against the real protobuf/OTel SDK.
    """

    def __init__(self, settings: Any, *, http_client=None):
        import httpx
        from langfuse._client.attributes import LangfuseOtelSpanAttributes

        reason = _configuration_error(settings)
        if reason:
            raise ExportError(reason)
        self.settings = settings
        self.attributes = LangfuseOtelSpanAttributes
        self.base_url = settings.langfuse_base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/api/public/otel/v1/traces"
        self.timeout = float(getattr(settings, "obs_export_timeout_seconds", 2.0))
        self.environment = safe_token(getattr(settings, "environment", "development"), fallback="development")
        self.release = safe_token(getattr(settings, "obs_release", None))
        self._auth = httpx.BasicAuth(_secret(settings.langfuse_public_key), _secret(settings.langfuse_secret_key))
        self._headers = {"Content-Type": "application/x-protobuf", "x-langfuse-ingestion-version": "4",
                         "x-langfuse-sdk-name": "lesson-assistant-otel", "x-langfuse-sdk-version": version("langfuse")}
        self._owns_http = http_client is None
        self.http = http_client or httpx.Client(timeout=self.timeout, follow_redirects=False,
                                                transport=httpx.HTTPTransport(retries=0))

    def _readable_span(self, event: Mapping[str, Any]):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import Event, ReadableSpan
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

        attributes = self.attributes
        metadata = sanitize_metadata(event.get("metadata"))
        if event.get("business_run_id"):
            metadata["business_run_id"] = safe_token(event["business_run_id"], fallback="redacted")
        if isinstance(event.get("execution_epoch"), int):
            metadata["execution_epoch"] = event["execution_epoch"]
        metadata["execution_status"] = event["status"]
        observation_type = {"llm": "generation", "prompt": "span", "parser": "span"}.get(
            event["run_type"], event["run_type"])
        values: dict[str, Any] = {
            attributes.OBSERVATION_TYPE: observation_type,
            attributes.OBSERVATION_LEVEL: {"info": "DEFAULT", "warning": "WARNING", "error": "ERROR"}[event["level"]],
            attributes.TRACE_NAME: safe_token(event.get("trace_name"), fallback="lesson.execute"),
            attributes.TRACE_PUBLIC: False,
            attributes.ENVIRONMENT: self.environment,
        }
        if getattr(self.settings, "obs_capture_mode", "metadata") == "full":
            from app.observability.content import configured_secrets, sanitize_content

            for name, key in (("input", attributes.OBSERVATION_INPUT), ("output", attributes.OBSERVATION_OUTPUT)):
                if name in event:
                    captured, _ = sanitize_content(_plain_snapshot(event[name]), max_bytes=content_limit(self.settings),
                                                    secrets=configured_secrets(self.settings))
                    values[key] = json.dumps(captured, ensure_ascii=False, separators=(",", ":"))
        # Trace/session fields are copied to every observation so Langfuse can filter/aggregate them.
        thread_id = metadata.get("thread_id")
        if thread_id:
            values[attributes.TRACE_SESSION_ID] = thread_id
        if self.release:
            values[attributes.RELEASE] = self.release
        for key, value in metadata.items():
            values[f"{attributes.OBSERVATION_METADATA}.{key}"] = value
        for key in ("business_run_id", "execution_epoch", "workflow", "release", "sampled", "sample_rate"):
            if key in metadata:
                values[f"{attributes.TRACE_METADATA}.{key}"] = metadata[key]
        code = metadata.get("error_code", metadata.get("code"))
        if code:
            values[attributes.OBSERVATION_STATUS_MESSAGE] = code
        events = []
        for item in event.get("events", ()):
            name = safe_token(item.get("name"))
            if name:
                events.append(Event(name, timestamp=_nanoseconds(item["time"])))
                if name == "new_token" and observation_type == "generation":
                    values[attributes.OBSERVATION_COMPLETION_START_TIME] = item["time"]
        if observation_type == "generation":
            model = metadata.get("model", metadata.get("model_name", metadata.get("ls_model_name")))
            if model:
                values[attributes.OBSERVATION_MODEL] = model
            # Demo and replay have no billable usage. Partial provider usage stays partial.
            if metadata.get("usage_known") and not metadata.get("is_demo") and not metadata.get("receipt_reused"):
                usage = {target: metadata[source] for source, target in (
                    ("input_tokens", "input"), ("output_tokens", "output"), ("total_tokens", "total"))
                    if source in metadata}
                if usage:
                    values[attributes.OBSERVATION_USAGE_DETAILS] = json.dumps(usage, separators=(",", ":"))
        trace_id = int(trace_hex(event["trace_id"]), 16)
        context = SpanContext(trace_id, int(span_hex(event["span_id"]), 16), False, TraceFlags(TraceFlags.SAMPLED))
        parent = None
        if event.get("parent_span_id"):
            parent = SpanContext(trace_id, int(span_hex(event["parent_span_id"]), 16), False, TraceFlags(TraceFlags.SAMPLED))
        else:
            values[attributes.IS_APP_ROOT] = True
        status = Status(StatusCode.ERROR, code or "operation_failed") if event["status"] == "failed" else Status(StatusCode.OK)
        return ReadableSpan(
            name=event["name"], context=context, parent=parent, attributes=values, events=events, status=status,
            resource=Resource({"service.name": "lesson-assistant"}),
            instrumentation_scope=InstrumentationScope("lesson-assistant.observability", "1"),
            start_time=_nanoseconds(event["started_at"]), end_time=_nanoseconds(event["ended_at"]),
        )

    def send_batch(self, operations: Sequence[Mapping[str, Any]]) -> None:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

        spans = [self._readable_span(event) for event in operations if event["phase"] == "end"]
        if not spans:
            return
        payload = encode_spans(spans).SerializeToString()
        # Response text, request headers and exceptions are never logged or copied into observations.
        with self.http.stream("POST", self.endpoint, content=payload, headers=self._headers,
                              auth=self._auth, timeout=self.timeout, follow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise ExportError("observability_http_failed", response.status_code)
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > 16384:
                    raise ExportError("observability_response_too_large")
                body.extend(chunk)
            if body:
                try:
                    if response.headers.get("content-type", "").split(";", 1)[0].strip() == "application/json":
                        # Langfuse v4 acknowledges queued ingestion with JSON, including for protobuf requests.
                        acknowledgement = json.loads(body)
                        if not isinstance(acknowledgement, dict):
                            raise ValueError("invalid acknowledgement")
                        partial = acknowledgement.get("partialSuccess", acknowledgement.get("partial_success", {}))
                        rejected = int(partial.get("rejectedSpans", partial.get("rejected_spans", 0)))
                    else:
                        result = ExportTraceServiceResponse.FromString(bytes(body))
                        rejected = result.partial_success.rejected_spans
                except Exception:
                    raise ExportError("observability_invalid_response") from None
                if rejected:
                    raise ExportError("observability_partial_rejection")

    def close(self) -> None:
        if self._owns_http:
            self.http.close()


class LangfuseExporter:
    """Application-owned completed-span queue and a single best-effort daemon sender."""

    def __init__(self, settings: Any, *, transport=None, retry_delay: float = 0.05):
        self.settings = settings
        self.transport = transport
        self.retry_delay = retry_delay
        self.capacity = max(1, int(getattr(settings, "obs_export_queue_size", 512)))
        self.max_event_bytes = export_event_budget(settings)
        self.queue: queue.Queue[bytes] = queue.Queue(self.capacity)
        self.disabled_reason = _configuration_error(settings)
        self.configured = self.disabled_reason is None
        self._closed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._deadline = float("inf")
        self._lock = threading.RLock()
        self._sent = self._failed = self._dropped = self._inflight = 0
        self._consecutive_failures = 0
        self._circuit_until = 0.0
        self._shutdown_timed_out = False
        self._last_diagnostic: dict[str, float] = {}

    def _diagnostic(self, code: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_diagnostic.get(code, float("-inf")) < 60:
                return
            self._last_diagnostic[code] = now
        logger.warning("%s", code, extra={"observability_code": code})

    def submit(self, event: Mapping[str, Any]) -> bool:
        # OTLP uploads completed spans atomically; starts and events remain immediate local records.
        if event.get("phase") != "end":
            return True
        with self._lock:
            if self._closed or self.disabled_reason or time.monotonic() < self._circuit_until:
                self._dropped += 1
                return False
            try:
                record = dict(event)
                if getattr(self.settings, "obs_capture_mode", "metadata") != "full":
                    record.pop("input", None)
                    record.pop("output", None)
                payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
                if len(payload) > self.max_event_bytes:
                    self._dropped += 1
                    self._diagnostic("observability_payload_dropped")
                    return False
                self.queue.put_nowait(payload)
                return True
            except (TypeError, ValueError, queue.Full):
                self._dropped += 1
                self._diagnostic("observability_queue_dropped")
                return False

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                return
            if self.disabled_reason:
                self._diagnostic(self.disabled_reason)
                return
            self._thread = threading.Thread(target=self._run, name="langfuse-exporter", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            if self.transport is None:
                try:
                    self.transport = SDKTransport(self.settings)
                except Exception:
                    self.disabled_reason = "observability_initialization_failed"
                    self._diagnostic(self.disabled_reason)
                    return
            while True:
                if self._stop.is_set() and (self.queue.empty() or time.monotonic() >= self._deadline):
                    break
                self.drain_once(wait=0.05)
        finally:
            self._discard_pending()
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:
                    self._diagnostic("observability_close_failed")

    def drain_once(self, *, wait: float = 0) -> None:
        """One bounded batch; called by sender (or synchronously by offline transport tests)."""
        batch = []
        try:
            batch.append(self.queue.get(timeout=wait))
        except queue.Empty:
            return
        # Content-bearing records are larger; keep their aggregate HTTP batch bounded too.
        batch_limit = min(32, max(1, 524288 // self.max_event_bytes))
        while len(batch) < batch_limit:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            self._inflight += len(batch)
        try:
            if self.disabled_reason or time.monotonic() < self._circuit_until:
                with self._lock:
                    self._dropped += len(batch)
                return
            operations = [json.loads(payload) for payload in batch]
            for attempt in range(2):
                if self._stop.is_set() and time.monotonic() >= self._deadline:
                    with self._lock:
                        self._dropped += len(batch)
                    return
                try:
                    self.transport.send_batch(operations)
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code in {400, 401, 403, 404}:
                        self.disabled_reason = ("observability_auth_failed" if status_code in {401, 403}
                                                else "observability_configuration_failed")
                        break
                    if attempt == 0:
                        self._stop.wait(self.retry_delay)
                        continue
                    break
                else:
                    with self._lock:
                        self._sent += len(batch)
                        self._consecutive_failures = 0
                        self._circuit_until = 0
                    return
            with self._lock:
                self._failed += len(batch)
                self._consecutive_failures += 1
                if self._consecutive_failures >= 5 and not self.disabled_reason:
                    self._circuit_until = time.monotonic() + 30
                    self._diagnostic("observability_circuit_open")
            self._diagnostic(self.disabled_reason or "observability_export_failed")
        finally:
            with self._lock:
                self._inflight -= len(batch)
            for _ in batch:
                self.queue.task_done()

    def _discard_pending(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                with self._lock:
                    self._dropped += 1
            except queue.Empty:
                return

    def close(self, timeout: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            timeout = max(0, float(timeout if timeout is not None else getattr(
                self.settings, "obs_shutdown_timeout_seconds", 5.0)))
            self._deadline = time.monotonic() + timeout
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                self._shutdown_timed_out = True
                self._diagnostic("observability_shutdown_timeout")
                self._discard_pending()
        else:
            self._discard_pending()

    def status(self) -> dict[str, Any]:
        with self._lock:
            enabled = self.configured and self.disabled_reason is None
            return {"provider": "langfuse", "enabled": enabled, "remote_enabled": enabled,
                    "configured": self.configured, "disabled_reason": self.disabled_reason,
                    "last_error_code": self.disabled_reason or next(reversed(self._last_diagnostic), None),
                    "capture_mode": getattr(self.settings, "obs_capture_mode", "metadata"),
                    "max_content_bytes": content_limit(self.settings),
                    "running": self._thread is not None and self._thread.is_alive(),
                    "accepting": enabled and not self._closed,
                    "project": safe_token(getattr(self.settings, "langfuse_project", None)),
                    "sent": self._sent, "failed": self._failed, "dropped": self._dropped,
                    "pending": self.queue.qsize() + self._inflight, "queue_capacity": self.capacity,
                    "queued": self.queue.qsize(), "capacity": self.capacity,
                    "consecutive_failures": self._consecutive_failures,
                    "circuit_open": time.monotonic() < self._circuit_until,
                    "shutdown_timed_out": self._shutdown_timed_out}
