"""Full capture is explicit, remote-only, bounded, and encoded as observation IO."""
import json
from types import SimpleNamespace

import httpx
import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from app.core.config import Settings
from app.observability import create_observer
from app.observability.langfuse import LangfuseExporter, SDKTransport


class RecordingExporter:
    def __init__(self):
        self.events = []

    def submit(self, event):
        self.events.append(event)


def config(mode="full", **overrides):
    values = dict(obs_capture_mode=mode, langfuse_enabled=True, obs_deployment_id="full-capture-test",
        langfuse_base_url="http://127.0.0.1:3100", langfuse_public_key="pk-lf-synthetic-key",
        langfuse_secret_key="sk-lf-synthetic-secret", llm_api_key="synthetic-provider-credential",
        obs_max_content_bytes=1024, obs_max_event_bytes=2048)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_settings_accept_full_without_changing_metadata_default():
    assert Settings(_env_file=None).obs_capture_mode == "metadata"
    assert Settings(_env_file=None, obs_capture_mode="full").obs_capture_mode == "full"
    assert LangfuseExporter(config()).status()["capture_mode"] == "full"
    assert LangfuseExporter(config()).configured


@pytest.mark.parametrize("mode, enabled, rate", [("metadata", True, 1), ("full", False, 1), ("full", True, 0)])
def test_content_is_not_captured_without_opt_in_and_sampled_remote_export(mode, enabled, rate):
    remote, local = RecordingExporter(), []
    observer = create_observer(config(mode, langfuse_enabled=enabled, obs_trace_sample_rate=rate),
                               local_sink=local.append, exporter=remote)
    with observer.execution("synthetic-run", 0) as root:
        root.set_input({"topic": "PRIVATE_INPUT"})
        root.set_output("PRIVATE_OUTPUT")
    assert "PRIVATE" not in json.dumps(local + remote.events)


def test_full_capture_is_remote_only_and_never_inherits_parent_content():
    remote, local = RecordingExporter(), []
    observer = create_observer(config(), local_sink=local.append, exporter=remote)
    messages = [{"role": "user", "content": "请为分数安排学习活动"}]
    with observer.execution("synthetic-run", 0) as root:
        root.set_input({"topic": "分数"})
        with observer.span("llm.request", run_type="llm") as request:
            request.set_input(messages)
            messages[0]["content"] = "不应出现在已捕获快照中"
            request.set_output("通过折纸理解平均分。")
        with observer.span("checkpoint.save"):
            pass
        root.set_output({"status": "completed"})
    ends = {event["name"]: event for event in remote.events if event["phase"] == "end"}
    assert ends["llm.request"]["input"][0]["content"] == "请为分数安排学习活动"
    assert ends["llm.request"]["output"] == "通过折纸理解平均分。"
    assert ends["lesson.execute"]["input"] == {"topic": "分数"}
    assert "input" not in ends["checkpoint.save"]
    assert "output" not in ends["checkpoint.save"]
    assert all("input" not in event and "output" not in event for event in local)
    assert "分数" not in json.dumps(local, ensure_ascii=False)


def test_full_content_limits_do_not_drop_identity_or_the_whole_span():
    remote, local = RecordingExporter(), []
    observer = create_observer(config(), local_sink=local.append, exporter=remote)
    with observer.execution("synthetic-run", 0) as root:
        root.set_input({"text": "输入正文" * 5000})
        root.set_output("输出正文" * 5000)
    end = next(event for event in remote.events if event["phase"] == "end")
    assert end["metadata"]["input_truncated"]
    assert end["metadata"]["output_truncated"]
    assert end["business_run_id"] == "synthetic-run"
    for name in ("input", "output"):
        assert len(json.dumps(end[name], ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert max(len(json.dumps(event).encode()) for event in local) <= 2048
    assert observer.payload_dropped == 0


@pytest.mark.parametrize("mode", ["metadata", "full"])
def test_otlp_uses_observation_input_output_only_in_full_mode(mode):
    requests = []
    remote = RecordingExporter()
    observer = create_observer(config(mode), exporter=remote)
    with observer.execution("synthetic-run", 0) as root:
        root.set_input({"topic": "学习分数", "api_key": "should-be-redacted"})
        root.set_output("模型回复 synthetic-provider-credential")
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))) as http:
        SDKTransport(config(mode), http_client=http).send_batch(remote.events)
    payload = ExportTraceServiceRequest.FromString(requests[0].content)
    spans = [span for resource in payload.resource_spans for scope in resource.scope_spans for span in scope.spans]
    attributes = {item.key: MessageToDict(item.value) for item in spans[0].attributes}
    if mode == "full":
        assert json.loads(attributes["langfuse.observation.input"]["stringValue"])["topic"] == "学习分数"
        assert "模型回复" in attributes["langfuse.observation.output"]["stringValue"]
        assert "should-be-redacted" not in str(payload)
        assert "synthetic-provider-credential" not in str(payload)
    else:
        assert "langfuse.observation.input" not in attributes
        assert "langfuse.observation.output" not in attributes


def test_full_exporter_accepts_bounded_content_larger_than_metadata_budget():
    exporter = LangfuseExporter(config(obs_max_content_bytes=4096))
    observer = create_observer(config(obs_max_content_bytes=4096), exporter=exporter)
    with observer.execution("synthetic-run", 0) as root:
        root.set_input("中文内容" * 300)
        root.set_output("中文回复" * 300)
    assert exporter.status()["queued"] == 1
    assert exporter.status()["dropped"] == 0

