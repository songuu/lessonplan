import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.observability import create_observer, current_observer, current_span


def configuration(**values):
    defaults = dict(langfuse_enabled=False, obs_deployment_id="test", obs_trace_sample_rate=1.0)
    defaults.update(values)
    return SimpleNamespace(**defaults)


class RecordingExporter:
    def __init__(self):
        self.events = []

    def submit(self, event):
        self.events.append(event)
        return True

    def start(self):
        pass

    def close(self, timeout=None):
        pass

    def status(self):
        return {"pending": 0}


def test_disabled_cloud_still_records_immutable_local_spans_without_sdk(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("langsmith", "langfuse", "opentelemetry"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    observer.start()
    with observer.execution("run-one", 0, draft_id="draft-one") as root:
        assert current_observer() is observer
        assert current_span() is root
        with observer.span("module.generate", metadata={"module_type": "intro"}) as child:
            child.set(validation_outcome="passed")
    observer.close()
    assert current_span() is None and current_observer() is None
    assert [event["phase"] for event in events] == ["start", "start", "end", "end"]
    assert events[1]["parent_span_id"] == root.span_id
    assert all(event["trace_id"] == root.trace_id for event in events)
    assert events[2]["metadata"]["validation_outcome"] == "passed"
    assert events[1]["metadata"]["thread_id"] == events[0]["metadata"]["thread_id"]
    assert len({event["event_id"] for event in events}) == 4
    UUID(root.span_id)
    with pytest.raises(TypeError):
        events[0]["phase"] = "end"
    with pytest.raises(TypeError):
        events[0]["metadata"]["workflow"] = "changed"


def test_default_capture_rejects_private_text_secrets_exceptions_and_unknown_fields():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with pytest.raises(ValueError):
        with observer.execution("run-one", 0):
            with observer.span("llm.request", run_type="llm", metadata={
                "prompt": "私人课题", "model": "sk-super-private-key-123456789", "route": "primary",
                "error": ValueError("password=private"), "module_type": "intro",
                "stage": "teacher@example.test", "filename": "private.docx",
            }) as span:
                span.fail("invalid error contains password=private")
                raise ValueError("私人课题 password=private")
    serialized = json.dumps(events, ensure_ascii=False)
    for forbidden in ("私人", "password", "sk-super", "teacher@", "private.docx"):
        assert forbidden not in serialized
    assert events[-2]["status"] == "failed"
    assert events[-2]["metadata"]["error_code"] == "operation_failed"


def test_snapshot_does_not_change_when_caller_updates_metadata():
    events = []
    metadata = {"route": "primary", "prompt": {"text": "private"}}
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.span("llm.request", metadata=metadata) as span:
        metadata["route"] = "backup"
        span.set(route="repaired")
    assert events[0]["metadata"]["route"] == "primary"
    assert events[-1]["metadata"]["route"] == "repaired"


def test_local_sink_failure_does_not_replace_business_error():
    def failed_sink(event):
        raise RuntimeError("private database failure")

    observer = create_observer(configuration(), local_sink=failed_sink)
    with pytest.raises(ValueError, match="business failure"):
        with observer.execution("run-one", 0):
            raise ValueError("business failure")
    assert observer.status()["local_dropped"] == 2


@pytest.mark.asyncio
async def test_concurrent_tasks_and_resumed_epochs_have_separate_contexts():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)

    async def execute(run_id, epoch):
        with observer.execution(run_id, epoch, draft_id="shared-draft") as root:
            await asyncio.sleep(0)
            with observer.span("quality.validate") as child:
                assert child.parent_span_id == root.span_id
                await asyncio.sleep(0)
            return root

    first, second, resumed = await asyncio.gather(execute("run-one", 0), execute("run-two", 0),
                                                  execute("run-one", 1))
    assert first.trace_id != second.trace_id != resumed.trace_id
    assert resumed.metadata["previous_trace_id"] == first.trace_id
    assert resumed.metadata["thread_id"] == first.metadata["thread_id"]
    assert current_span() is None


def test_root_id_is_stable_per_deployment_run_epoch_and_nested_execution_detaches_parent():
    observer = create_observer(configuration())
    with observer.execution("run-one", 0) as outer:
        with observer.execution("run-one", 1) as inner:
            assert inner.parent_span_id is None
        assert current_span() is outer
    with observer.execution("run-one", 0) as repeat:
        assert repeat.trace_id == outer.trace_id == observer.trace_id_for("run-one", 0)
    other = create_observer(configuration(obs_deployment_id="other"))
    assert other.trace_id_for("run-one", 0) != outer.trace_id


@pytest.mark.asyncio
async def test_cancellation_preserves_exception_and_records_cancellation():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with pytest.raises(asyncio.CancelledError):
        with observer.execution("run-one", 0):
            raise asyncio.CancelledError()
    assert events[-1]["status"] == "cancelled"
    assert current_span() is None


@pytest.mark.parametrize("status", ["lease_lost", "interrupted"])
def test_explicit_business_status_is_not_overwritten_by_normal_return(status):
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.execution("run-one", 0) as span:
        span.set(status=status, outcome=status)
    assert events[-1]["status"] == status
    assert events[-1]["level"] == "warning"


def test_sampling_never_disables_local_logs_and_recovery_uses_same_decision():
    events = []
    exporter = RecordingExporter()
    observer = create_observer(configuration(langfuse_enabled=True, obs_trace_sample_rate=0),
                               local_sink=events.append, exporter=exporter)
    for epoch in (0, 1):
        with observer.execution("run-one", epoch):
            with observer.span("llm.request", run_type="llm"):
                pass
    assert len(events) == 8
    assert not exporter.events
    assert all(event["metadata"]["sampled"] is False for event in events)


def test_usage_and_first_token_are_only_attached_to_actual_llm_span():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.execution("run-one", 0) as root:
        root.usage({"prompt_tokens": 50, "completion_tokens": 20})
        with observer.span("llm.request", run_type="llm") as request:
            request.usage({"prompt_tokens": 0, "completion_tokens": 5})
            request.event("new_token")
            request.event("new_token")
        with observer.span("receipt.replay", metadata={"receipt_reused": True}) as replay:
            replay.usage({"prompt_tokens": 50, "completion_tokens": 20})
    end = next(event for event in events if event["phase"] == "end" and event["name"] == "llm.request")
    assert end["metadata"]["input_tokens"] == 0
    assert end["metadata"]["output_tokens"] == 5
    assert end["metadata"]["total_tokens"] == 5
    assert end["metadata"]["usage_known"] is True
    assert len(end["events"]) == 1
    assert end["events"][0]["name"] == "new_token"
    assert all("input_tokens" not in event["metadata"] for event in events if event["run_type"] != "llm")


def test_missing_partial_and_demo_usage_are_explicit():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.span("llm.request", run_type="llm") as unknown:
        unknown.usage({})
    with observer.span("llm.request", run_type="llm") as partial:
        partial.usage({"prompt_tokens": 3})
    with observer.span("llm.request", run_type="llm", metadata={"is_demo": True}) as demo:
        demo.usage({"prompt_tokens": 3, "completion_tokens": 2})
    end_events = [event for event in events if event["phase"] == "end"]
    assert end_events[0]["metadata"]["usage_known"] is False
    assert "input_tokens" not in end_events[0]["metadata"]
    assert end_events[1]["metadata"]["input_tokens"] == 3
    assert "total_tokens" not in end_events[1]["metadata"]
    assert end_events[2]["metadata"]["cost_status"] == "demo"


def test_metadata_boundary_handles_huge_numbers_nonfinite_values_and_credentials():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.execution("run-one", 0) as root:
        original_thread = root.metadata["thread_id"]
        root.set(thread_id="attacker-thread", sampled=False, sample_rate=0)
        with observer.span("safe.operation", metadata={
            "input_tokens": 10**1000, "first_token_ms": float("nan"), "route": "pk-lf-credential-should-not-leak",
            "model": "lsv2_credential_should_not_leak", "count": True, "quality_attempt": 1.5,
        }):
            pass
    assert events[-1]["metadata"]["thread_id"] == original_thread
    assert events[-1]["metadata"]["sampled"] is True
    text = json.dumps(events)
    assert "credential" not in text
    assert "NaN" not in text
    assert not any(key in events[1]["metadata"] for key in ("count", "input_tokens", "quality_attempt"))


def test_identity_and_usage_are_inherited_without_copying_ancestor_token_totals():
    events = []
    observer = create_observer(configuration(), local_sink=events.append)
    with observer.execution("run-one", 0):
        with observer.span("generation.call", metadata={"invocation_id": "invocation-one", "call_id": "call-one",
                "step_key": "module-one", "module_id": "module-one", "quality_attempt": 1}):
            with observer.span("llm.request", run_type="llm") as child:
                assert child.metadata["invocation_id"] == "invocation-one"
                assert child.metadata["quality_attempt"] == 1
                child.usage({"input_tokens": 3, "output_tokens": 4})
                with observer.span("receipt.replay", metadata={"receipt_reused": True, "input_tokens": 90}) as replay:
                    replay.set(output_tokens=90)
                    assert "input_tokens" not in replay.metadata
                    assert "output_tokens" not in replay.metadata
    assert events[-1]["metadata"].get("input_tokens") is None


def test_payload_and_trace_caps_reserve_actual_request_slots():
    events = []
    observer = create_observer(configuration(obs_max_event_bytes=2048), local_sink=events.append)
    with observer.execution("run-one", 0):
        for _ in range(400):
            with observer.span("quality.validate", metadata={"route": "x" * 120}):
                pass
        for _ in range(32):
            with observer.span("llm.request", run_type="llm"):
                pass
    starts = [event for event in events if event["phase"] == "start"]
    assert len(starts) <= 256
    assert sum(event["run_type"] == "llm" for event in starts) == 32
    assert len([event for event in events if event["metadata"].get("event_name") == "span_limit_reached"]) == 1
    assert max(len(json.dumps(event).encode()) for event in events) <= 2048
