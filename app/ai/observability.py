"""Explicit, metadata-only instrumentation for the existing runtime.

Context is established by a claimed execution, never by an untrusted HTTP header.
"""
from contextlib import nullcontext
from functools import wraps

from app.observability import current_observer, current_span


def step(name, *, run_type="chain", span_id=None, **metadata):
    observer = current_observer()
    if observer is None:
        return nullcontext(None)
    return observer.span(name, run_type=run_type, metadata=metadata, span_id=span_id)


def observe_step(name, *, run_type="chain"):
    def decorate(function):
        @wraps(function)
        async def observed(*args, **kwargs):
            with step(name, run_type=run_type):
                return await function(*args, **kwargs)
        return observed
    return decorate


def mark(**metadata):
    span = current_span()
    if span is not None:
        span.set(**metadata)
