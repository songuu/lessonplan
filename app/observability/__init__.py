"""Minimal observability API; optional Langfuse resources start with process lifecycle."""

from app.observability.context import current_observer, current_span
from app.observability.tracer import Observer, Span


def create_observer(settings, *, local_sink=None, exporter=None) -> Observer:
    if exporter is None and getattr(settings, "langfuse_enabled", False):
        from app.observability.langfuse import LangfuseExporter

        exporter = LangfuseExporter(settings)
    return Observer(settings, local_sink=local_sink, exporter=exporter)


__all__ = ["Observer", "Span", "create_observer", "current_observer", "current_span"]
