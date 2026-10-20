import threading
import time
from types import SimpleNamespace

from app.observability import create_observer
from app.observability.langfuse import LangfuseExporter


def configuration(**values):
    defaults = dict(langfuse_enabled=True, langfuse_public_key="test-key", langfuse_secret_key="test-secret",
                    langfuse_base_url="http://127.0.0.1:3100", langfuse_project="test-project",
                    obs_deployment_id="test", obs_capture_mode="metadata",
                    obs_export_queue_size=8, obs_shutdown_timeout_seconds=0.2,
                    obs_export_timeout_seconds=0.1, obs_trace_sample_rate=1)
    defaults.update(values)
    return SimpleNamespace(**defaults)


class Transport:
    def __init__(self):
        self.batches = []
        self.thread_names = []
        self.closed = False

    def send_batch(self, operations):
        self.thread_names.append(threading.current_thread().name)
        self.batches.append(operations)

    def close(self):
        self.closed = True


def test_remote_sender_is_lifecycle_managed_and_never_sends_in_business_thread():
    transport = Transport()
    exporter = LangfuseExporter(configuration(), transport=transport)
    observer = create_observer(configuration(), exporter=exporter)
    with observer.execution("run-one", 0):
        with observer.span("llm.request", run_type="llm") as span:
            span.usage({"prompt_tokens": 3, "completion_tokens": 2})
    assert not transport.batches
    observer.start()
    observer.start()
    observer.close(timeout=1)
    observer.close(timeout=1)
    assert transport.closed
    assert all(name == "langfuse-exporter" for name in transport.thread_names)
    assert sum(len(batch) for batch in transport.batches) == 2
    assert exporter.status()["sent"] == 2


def test_queue_overflow_drops_atomic_completed_spans_without_partial_updates():
    transport = Transport()
    exporter = LangfuseExporter(configuration(obs_export_queue_size=1), transport=transport)
    observer = create_observer(configuration(), exporter=exporter)
    with observer.execution("run-one", 0):
        with observer.span("child.operation"):
            pass
    assert exporter.status()["pending"] == 1
    assert exporter.status()["dropped"] == 1
    observer.start()
    observer.close(timeout=1)
    assert sum(len(batch) for batch in transport.batches) == 1
    assert transport.batches[0][0]["phase"] == "end"


def test_blocked_cloud_and_shutdown_have_bounded_business_latency():
    entered = threading.Event()
    unblock = threading.Event()

    class BlockedTransport(Transport):
        def send_batch(self, operations):
            entered.set()
            unblock.wait(2)
            super().send_batch(operations)

    exporter = LangfuseExporter(configuration(), transport=BlockedTransport())
    observer = create_observer(configuration(), exporter=exporter)
    observer.start()
    with observer.execution("run-one", 0):
        pass
    assert entered.wait(1)
    before = time.monotonic()
    for _ in range(100):
        with observer.span("local.operation"):
            pass
    assert time.monotonic() - before < 0.2
    before = time.monotonic()
    observer.close(timeout=0.02)
    assert time.monotonic() - before < 0.15
    assert exporter.status()["shutdown_timed_out"]
    unblock.set()


def test_retry_is_finite_and_auth_disables_cloud_without_leaking_message(caplog):
    class AuthError(Exception):
        status_code = 401

    class FailedTransport(Transport):
        attempts = 0

        def send_batch(self, operations):
            self.attempts += 1
            raise AuthError("Bearer sk-private-secret and private teacher content")

    transport = FailedTransport()
    exporter = LangfuseExporter(configuration(), transport=transport)
    observer = create_observer(configuration(), exporter=exporter)
    observer.start()
    with observer.execution("run-one", 0):
        pass
    observer.close(timeout=1)
    assert transport.attempts == 1
    assert exporter.status()["disabled_reason"] == "observability_auth_failed"
    assert "private" not in caplog.text
    assert "Bearer" not in caplog.text


def test_transient_failure_retries_once_and_opens_circuit_after_five_failed_batches():
    class FailedTransport(Transport):
        attempts = 0

        def send_batch(self, operations):
            self.attempts += 1
            raise ConnectionError("private connection detail")

    transport = FailedTransport()
    exporter = LangfuseExporter(configuration(), transport=transport, retry_delay=0)
    observer = create_observer(configuration(), exporter=exporter)
    for index in range(5):
        with observer.execution(f"run-{index}", 0):
            pass
        exporter.drain_once()
    assert transport.attempts == 10
    assert exporter.status()["circuit_open"]
    with observer.execution("after-circuit", 0):
        pass
    assert transport.attempts == 10
    assert exporter.status()["failed"] == 5


def test_missing_configuration_and_unsupported_capture_mode_disable_only_remote():
    for settings in (configuration(langfuse_secret_key=""), configuration(obs_capture_mode="full")):
        local = []
        observer = create_observer(settings, local_sink=local.append)
        observer.start()
        with observer.execution("run-one", 0):
            pass
        observer.close()
        assert len(local) == 2
        assert observer.status()["remote_enabled"] is False
        assert observer.status()["disabled_reason"]
