"""Content capture follows actual transports and committed business facts."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.ai.gateway import GatewayError, LLMGateway
from app.ai.runtime import LessonRuntime
from app.db.models import ModuleVersion, Run
from app.observability import create_observer
from tests.test_observability import RecordingExporter


def config(mode="full", **overrides):
    values = dict(ai_provider="openai", llm_model="test-model", llm_api_key="synthetic-provider-credential",
        llm_base_url="https://provider.invalid/v1", obs_deployment_id="full-flow-test",
        obs_capture_mode=mode, langfuse_enabled=True, quality_retry_limit=1)
    values.update(overrides)
    return SimpleNamespace(**values)


def observe(settings):
    remote, local = RecordingExporter(), []
    return create_observer(settings, local_sink=local.append, exporter=remote), remote, local


def ended(remote, name):
    return [event for event in remote.events if event["name"] == name and event["phase"] == "end"]


def response(body="实际模型原始回复"):
    return httpx.Response(200, json={"choices": [{"message": {"content": body}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})


@pytest.mark.parametrize("mode", ["metadata", "full"])
async def test_gateway_captures_exact_sent_messages_and_raw_response_remotely_only(mode):
    settings = config(mode)
    observer, remote, local = observe(settings)
    requests, messages = [], [{"role": "system", "content": "实际系统提示词"},
                              {"role": "user", "content": "PRIVATE_TEACHER_INPUT"}]

    def send(request):
        requests.append(json.loads(request.content))
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with observer.execution(str(uuid4()), 0):
            result = await LLMGateway(settings, client=client).complete(messages, route="one_click")
    assert len(requests) == len(ended(remote, "llm.request")) == 1
    event = ended(remote, "llm.request")[0]
    if mode == "full":
        assert list(event["input"]) == requests[0]["messages"] == messages
        assert event["output"] == result.content == "实际模型原始回复"
    else:
        assert "input" not in event and "output" not in event
        assert "PRIVATE_TEACHER_INPUT" not in json.dumps(remote.events)
    assert "PRIVATE_TEACHER_INPUT" not in json.dumps(local)
    assert "实际模型原始回复" not in json.dumps(local, ensure_ascii=False)
    assert settings.llm_api_key not in json.dumps(remote.events + local)


async def test_full_fallback_has_failed_attempt_input_without_fabricated_output():
    settings = config(llm_fallback_model="backup-model")
    observer, remote, local = observe(settings)
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(429) if len(requests) == 1 else response("备用模型原始回复")

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with observer.execution(str(uuid4()), 0):
            await LLMGateway(settings, client=client).complete(
                [{"role": "user", "content": "实际用户消息"}], route="one_click", strict_outcome=True)
    primary, backup = ended(remote, "llm.request")
    assert len(requests) == 2
    assert list(primary["input"]) == list(backup["input"]) == [{"role": "user", "content": "实际用户消息"}]
    assert primary["status"] == "failed" and "output" not in primary
    assert backup["output"] == "备用模型原始回复"
    assert "实际用户消息" not in json.dumps(local, ensure_ascii=False)


async def test_full_incomplete_stream_keeps_input_without_success_output_or_an_extra_request():
    settings = config(llm_fallback_model="backup-model")
    observer, remote, _ = observe(settings)
    requests = []
    body = 'data: {"choices":[{"delta":{"content":"未完成的部分内容"}}]}\n\n'

    def send(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        with observer.execution(str(uuid4()), 0), pytest.raises(GatewayError) as error:
            await LLMGateway(settings, client=client).complete(
                [{"role": "user", "content": "需要完整回复"}], route="one_click", strict_outcome=True)
    event = ended(remote, "llm.request")[0]
    assert error.value.outcome_unknown and len(requests) == 1
    assert list(event["input"]) == [{"role": "user", "content": "需要完整回复"}]
    assert event["status"] == "failed" and "output" not in event


async def test_full_receipt_replay_has_content_without_new_llm_request_or_usage():
    from tests.test_invocation_runtime import INPUT, MODULES, Journal, wrapped

    settings = config()
    observer, remote, local = observe(settings)
    saved, requests, journal = {}, [], Journal()
    raw = wrapped("goals", "学生能解释平均分。")

    async def checkpoint(key, payload):
        if key.startswith("module:"):
            raise OSError("synthetic checkpoint failure")
        saved[key] = copy.deepcopy(payload)

    def send(request):
        requests.append(request)
        return response(raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        runtime = LessonRuntime(settings, gateway=LLMGateway(settings, client=client))
        run_id = str(uuid4())
        with observer.execution(run_id, 0), pytest.raises(OSError):
            await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, checkpoint_sink=checkpoint)
        with observer.execution(run_id, 1):
            result = await runtime.generate(INPUT, MODULES[:1], invocation_store=journal, resume=saved)
    assert len(requests) == len(ended(remote, "llm.request")) == 1
    replay = ended(remote, "receipt.replay")[0]
    assert replay["output"] == raw
    assert "total_tokens" not in replay["metadata"]
    assert result["modules"][0]["teacher_view_content"] == "学生能解释平均分。"
    assert ended(remote, "runtime.generate")[-1]["output"]["modules"][0]["teacher_view_content"] == "学生能解释平均分。"
    assert "学生能解释平均分" not in json.dumps(local, ensure_ascii=False)


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_root_output_is_committed_result_and_never_uncommitted_model_result(client, monkeypatch, commit_fails):
    from app.services import lessons
    from tests.test_execution_recovery import frozen_run

    _, item = await frozen_run(client)
    observer, remote, local = observe(config())
    client.app.state.observer = observer
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        run.payload = {**run.payload, "system_settings": {"private": "UNEXPECTED_SYSTEM_VALUE"}}
        await session.commit()

    async def generate(data, modules, **options):
        return {"modules": [{"module_id": module["module_id"], "teacher_view_content": "已提交的教学正文",
                             "canonical_content": {"markdown": "已提交的教学正文"}, "findings": []}
                            for module in modules], "calls": [], "ignored_raw": "RAW_NOT_COMMITTED"}

    client.app.state.runtime.generate = generate
    if commit_fails:
        async def interrupted(*args, **kwargs):
            raise OSError("PRIVATE_WRITE_FAILURE")
        monkeypatch.setattr(lessons, "write_version", interrupted)
    await client.app.state.dispatcher._execute(item["run_id"])
    root = ended(remote, "lesson.execute")[0]
    assert root["input"]["lesson"]["topic"] == "分数的初步认识"
    assert "UNEXPECTED_SYSTEM_VALUE" not in json.dumps(remote.events)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        versions = (await session.scalars(select(ModuleVersion).where(ModuleVersion.draft_id == item["draft_id"]))).all()
        assert root["output"]["status"] == run.status == ("failed" if commit_fails else "completed")
        if commit_fails:
            assert not versions and "modules" not in root["output"]
            assert root["output"]["error_code"] == "WORKFLOW_FAILED"
        else:
            actual = {version.id: version.teacher_view_content for version in versions}
            assert {module["version_id"]: module["teacher_view_content"] for module in root["output"]["modules"]} == actual
            assert root["output"]["result"] == run.result
    assert "RAW_NOT_COMMITTED" not in json.dumps(root, ensure_ascii=False)
    assert "PRIVATE_WRITE_FAILURE" not in json.dumps(remote.events)
    assert "分数的初步认识" not in json.dumps(local, ensure_ascii=False)
    assert "已提交的教学正文" not in json.dumps(local, ensure_ascii=False)


@pytest.mark.parametrize("mode", ["metadata", "full"])
async def test_optional_result_readback_is_skipped_in_metadata_and_cannot_fail_business_work(mode):
    from app.services.lessons import WorkflowExecutor

    observer, remote, _ = observe(config(mode))
    attempts = []

    def unavailable_session():
        attempts.append(True)
        raise OSError("PRIVATE_DATABASE_READ_FAILURE")

    app = SimpleNamespace(state=SimpleNamespace(db=SimpleNamespace(session_factory=unavailable_session)))
    executor = WorkflowExecutor(app)
    with observer.execution("synthetic-run", 0) as root:
        await executor.capture_committed_output("synthetic-run", 0, root)
    assert len(attempts) == (1 if mode == "full" else 0)
    event = ended(remote, "lesson.execute")[0]
    assert "output" not in event
    if mode == "full":
        assert event["metadata"]["observability_error_code"] == "CONTENT_RESULT_UNAVAILABLE"
    assert "PRIVATE_DATABASE_READ_FAILURE" not in json.dumps(remote.events)


async def test_result_readback_uses_run_version_facts_even_when_draft_points_to_a_later_edit(client):
    from app.db.models import Draft
    from app.services.lessons import WorkflowExecutor, write_version
    from tests.test_execution_recovery import frozen_run

    _, item = await frozen_run(client)

    async def generate(data, modules, **options):
        return {"modules": [{"module_id": module["module_id"], "teacher_view_content": "本次提交的正文",
                             "findings": []} for module in modules], "calls": []}

    client.app.state.runtime.generate = generate
    await client.app.state.dispatcher._execute(item["run_id"])
    async with client.app.state.db.session_factory() as session:
        draft = await session.get(Draft, item["draft_id"])
        previous_id = draft.current_module_version_map[draft.modules[0]["module_id"]]
        await write_version(session, draft, draft.modules[0]["module_id"], "后续教师手动修改的正文", source="teacher_edit")
        await session.commit()
    observer, remote, _ = observe(config())
    with observer.execution(item["run_id"], 0) as root:
        await WorkflowExecutor(client.app).capture_committed_output(item["run_id"], 0, root)
    result = ended(remote, "lesson.execute")[0]["output"]
    assert result["modules"][0]["version_id"] == previous_id
    assert result["modules"][0]["teacher_view_content"] == "本次提交的正文"
    assert "后续教师手动修改的正文" not in json.dumps(result, ensure_ascii=False)
