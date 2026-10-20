"""Task-local tracing context; no SDK or process resources are created on import."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from app.observability.tracer import Observer, Span

TRACE_NAMESPACE = UUID("caf7c26b-71a6-5fb7-a345-7dd3e3301186")
THREAD_NAMESPACE = UUID("b7942fd4-e6ca-58a9-bda8-2b0d5e380109")
SCHEMA_VERSION = "1"

observer_context: ContextVar[Observer | None] = ContextVar("lesson_observer", default=None)
span_context: ContextVar[Span | None] = ContextVar("lesson_span", default=None)


def current_observer() -> Observer | None:
    return observer_context.get()


def current_span() -> Span | None:
    return span_context.get()


def execution_trace_id(deployment_id: str, run_id: str, epoch: int) -> str:
    return str(uuid5(TRACE_NAMESPACE, f"{deployment_id}:{run_id}:{epoch}:{SCHEMA_VERSION}"))


def lesson_thread_id(deployment_id: str, run_id: str, draft_id: str | None, tenant_id: str | None) -> str:
    identity = f"draft:{tenant_id or ''}:{draft_id}" if draft_id else f"run:{run_id}"
    return str(uuid5(THREAD_NAMESPACE, f"{deployment_id}:{identity}:{SCHEMA_VERSION}"))
