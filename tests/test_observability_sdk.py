import json
from importlib.metadata import version
from types import SimpleNamespace

import httpx
import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from pydantic import SecretStr

from app.observability import create_observer
from app.observability.langfuse import ExportError, SDKTransport, span_hex, trace_hex


def configuration():
    return SimpleNamespace(langfuse_enabled=True, langfuse_public_key="pk-lf-explicit-test-key",
                           langfuse_secret_key=SecretStr("sk-lf-explicit-test-key"),
                           langfuse_base_url="http://127.0.0.1:3100", obs_deployment_id="test",
                           obs_export_timeout_seconds=0.25, obs_capture_mode="metadata")


def attributes_of(span):
    return {attribute.key: MessageToDict(attribute.value) for attribute in span.attributes}


def test_locked_sdk_otel_contract_explicit_parameters_hierarchy_usage_and_first_token(monkeypatch):
    from opentelemetry.trace import get_tracer_provider

    assert version("langfuse") == "4.18.0"
    original_provider = get_tracer_provider()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "ambient-key-never-used")
    monkeypatch.setenv("LANGFUSE_HOST", "https://ambient.test")
    monkeypatch.setenv("LANGFUSE_SAMPLE_RATE", "0")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private.key=private-content")
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, content=b"")

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        observer = create_observer(configuration(), local_sink=events.append)
        with observer.execution("run-one", 0) as root:
            with observer.span("llm.request", run_type="llm", metadata={
                "provider": "openai_compatible", "model": "custom-model",
            }) as request:
                request.event("new_token")
                request.usage({"prompt_tokens": 4, "completion_tokens": 6})
        transport.send_batch(events)
        assert len(requests) == 1
        sent = requests[0]
        assert (sent.method, str(sent.url)) == ("POST", "http://127.0.0.1:3100/api/public/otel/v1/traces")
        assert sent.extensions["timeout"]["read"] == 0.25
        assert sent.headers["authorization"].startswith("Basic ")
        assert sent.headers["x-langfuse-ingestion-version"] == "4"
        assert sent.headers["content-type"] == "application/x-protobuf"
        payload = ExportTraceServiceRequest.FromString(sent.content)
        spans = [span for resource in payload.resource_spans for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 2
        llm = next(span for span in spans if span.name == "llm.request")
        assert llm.parent_span_id.hex() == span_hex(root.span_id)
        assert llm.trace_id.hex() == trace_hex(root.trace_id)
        assert llm.span_id.hex() == span_hex(request.span_id)
        metadata = attributes_of(llm)
        assert metadata["langfuse.observation.type"] == {"stringValue": "generation"}
        assert json.loads(metadata["langfuse.observation.usage_details"]["stringValue"]) == {
            "input": 4, "output": 6, "total": 10,
        }
        assert metadata["langfuse.observation.model.name"] == {"stringValue": "custom-model"}
        assert metadata["langfuse.observation.completion_start_time"]["stringValue"]
        assert metadata["session.id"]["stringValue"] == root.metadata["thread_id"]
        assert llm.end_time_unix_nano >= llm.start_time_unix_nano > 0
        assert llm.events[0].name == "new_token"
        assert "ambient" not in str(payload)
        assert "private" not in str(payload)
        assert "langfuse.observation.input" not in metadata
        assert "langfuse.observation.output" not in metadata
        assert not hasattr(transport, "tracer_provider")
        assert get_tracer_provider() is original_provider
        transport.close()


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_sdk_transport_failure_is_visible_and_performs_exactly_one_http_attempt(status_code):
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(status_code, content=b'{"private":"do not log remote response"}')

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        observer = create_observer(configuration(), local_sink=events.append)
        with observer.execution("run-one", 0):
            pass
        with pytest.raises(ExportError) as captured:
            transport.send_batch(events)
        assert captured.value.status_code == status_code
        assert str(captured.value) == "observability_http_failed"
        assert len(requests) == 1
        transport.close()


def test_otlp_partial_usage_demo_and_replay_never_invent_or_duplicate_billable_tokens():
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        observer = create_observer(configuration(), local_sink=events.append)
        with observer.execution("run-one", 0):
            with observer.span("partial.request", run_type="llm") as partial:
                partial.usage({"input_tokens": 12})
            with observer.span("unknown.request", run_type="llm") as unknown:
                unknown.usage({})
            with observer.span("demo.request", run_type="llm", metadata={"is_demo": True}) as demo:
                demo.usage({"input_tokens": 10, "output_tokens": 20})
            with observer.span("receipt.replay", metadata={"receipt_reused": True}) as replay:
                replay.usage({"input_tokens": 10, "output_tokens": 20})
        transport.send_batch(events)
    payload = ExportTraceServiceRequest.FromString(requests[0].content)
    spans = {span.name: span for resource in payload.resource_spans for scope in resource.scope_spans for span in scope.spans}
    usage_key = "langfuse.observation.usage_details"
    assert json.loads(attributes_of(spans["partial.request"])[usage_key]["stringValue"]) == {"input": 12}
    for name in ("unknown.request", "demo.request", "receipt.replay", "lesson.execute"):
        assert usage_key not in attributes_of(spans[name])


def test_otlp_partial_rejection_is_a_controlled_failure_without_remote_response_text():
    rejected = ExportTraceServiceResponse()
    rejected.partial_success.rejected_spans = 1
    rejected.partial_success.error_message = "private server detail"
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=rejected.SerializeToString()))) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        with create_observer(configuration(), local_sink=events.append).execution("run-one", 0):
            pass
        with pytest.raises(ExportError, match="^observability_partial_rejection$"):
            transport.send_batch(events)


@pytest.mark.parametrize("response_body", [{}, {"id": "job-id", "name": "otel-ingestion"}])
def test_langfuse_json_enqueue_acknowledgement_is_accepted(response_body):
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_body))) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        with create_observer(configuration(), local_sink=events.append).execution("run-one", 0):
            pass
        transport.send_batch(events)


def test_json_partial_rejection_remains_a_failure():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"partialSuccess": {"rejectedSpans": "1", "errorMessage": "PRIVATE"}}))) as http:
        transport = SDKTransport(configuration(), http_client=http)
        events = []
        with create_observer(configuration(), local_sink=events.append).execution("run-one", 0):
            pass
        with pytest.raises(ExportError, match="^observability_partial_rejection$"):
            transport.send_batch(events)
