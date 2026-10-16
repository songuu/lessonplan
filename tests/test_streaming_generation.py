import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app.ai.gateway import Completion
from app.ai.runtime import LessonRuntime
from tests.conftest import login

BLUEPRINT = {"topic": "分数", "version": 1, "objectives": ["解释分数"]}


def replay(events, module_id):
    content = ""
    for kind, payload in events:
        if kind == "module_delta" and payload["module_id"] == module_id:
            content = payload["delta"] if payload.get("replace_previous") else content + payload["delta"]
    return content


@pytest.mark.asyncio
async def test_outline_streams_readable_stages_before_provider_finishes():
    stages = [{"stage_id": "a", "title": "观察分数", "duration_min": 10, "objective": '解释“平均分”与\\符号'},
              {"stage_id": "b", "title": "应用分数", "duration_min": 30, "objective": "独立解决问题"}]
    events = []

    class Gateway:
        async def complete(self, messages, **options):
            callback = options.get("on_delta")
            assert callback is not None, "大纲必须消费Provider增量，而不是等完整JSON"
            first = "```json\n[" + json.dumps(stages[0], ensure_ascii=False) + ","
            for offset in range(0, len(first), 7):
                await callback(first[offset:offset + 7])
            assert "观察分数（10分钟）" in replay(events, "process")
            assert "平均分" in replay(events, "process")
            assert "stage_id" not in replay(events, "process")
            assert not any(kind == "module_completed" for kind, _ in events)
            rest = json.dumps(stages[1], ensure_ascii=False) + "]\n```"
            await callback(rest)
            return Completion(first + rest, {"component_name": "structured", "call_outcome": "success"})

    runtime = LessonRuntime(SimpleNamespace(ai_provider="mock"), gateway=Gateway())
    result = await runtime.generate({"topic": "分数", "generation_mode": "interactive"},
        [{"module_id": "process", "module_type": "teaching_process"}], blueprint=BLUEPRINT,
        stage="outline", event_sink=lambda kind, payload: events.append((kind, payload)))
    assert result["modules"][0]["canonical_content"]["outline"] == stages
    assert replay(events, "process").strip() == result["modules"][0]["teacher_view_content"]


@pytest.mark.asyncio
async def test_structured_provider_retry_replaces_abandoned_partial_content():
    events = []

    class Gateway:
        async def complete(self, messages, **options):
            await options["on_delta"]("废弃的主模型片段")
            assert options.get("on_reset") is not None, "备用模型必须重置临时正文"
            await options["on_reset"]()
            await options["on_delta"]("教学目标：")
            assert replay(events, "goals") == "教学目标："
            await options["on_delta"]("学生能解释平均分。")
            return Completion("教学目标：学生能解释平均分。", {"component_name": "structured", "fallback_used": True})

    result = await LessonRuntime(SimpleNamespace(ai_provider="mock"), gateway=Gateway()).generate(
        {"topic": "分数", "generation_mode": "interactive"},
        [{"module_id": "goals", "module_type": "teaching_objectives"}], blueprint=BLUEPRINT,
        event_sink=lambda kind, payload: events.append((kind, payload)))
    assert replay(events, "goals") == result["modules"][0]["teacher_view_content"]
    assert "废弃" not in replay(events, "goals")


@pytest.mark.asyncio
async def test_one_click_json_fallback_publishes_completed_modules_before_response_end():
    events = []
    modules = [{"module_id": "a", "module_type": "teaching_objectives"},
               {"module_id": "b", "module_type": "teaching_methods"}]

    class Gateway:
        async def complete(self, messages, **options):
            if options["route"] == "one_click" and "JSON" not in messages[0]["content"]:
                await options["on_delta"]("错误的模块边界")
                return Completion("错误的模块边界", {"component_name": "one_click"})
            callback = options.get("on_delta")
            assert callback is not None, "JSON兜底也需增量转译为教师正文"
            first = '{"modules":[{"module_id":"a","content":"学生能解释平均分。"},'
            await callback(first)
            assert replay(events, "a") == "学生能解释平均分。"
            assert not any(kind == "module_started" and payload["module_id"] == "b" for kind, payload in events)
            rest = '{"module_id":"b","content":"教师引导学生折纸与解释。"}]}'
            await callback(rest)
            return Completion(first + rest, {"component_name": "one_click"})

    result = await LessonRuntime(SimpleNamespace(ai_provider="mock"), gateway=Gateway()).generate(
        {"topic": "分数", "generation_mode": "one_click"}, modules,
        event_sink=lambda kind, payload: events.append((kind, payload)))
    assert [replay(events, m["module_id"]) for m in modules] == [m["teacher_view_content"] for m in result["modules"]]


@pytest.mark.asyncio
async def test_activity_retry_keeps_previous_stages_and_discards_only_the_current_attempt():
    events = []
    stages = [{"stage_id": "a", "title": "观察", "duration_min": 10},
              {"stage_id": "b", "title": "迁移", "duration_min": 30}]

    class Gateway:
        async def complete(self, messages, **options):
            current = json.loads(messages[-1]["content"])["current_stage"]
            text = f"### {current['title']}（{current['duration_min']}分钟）\n\n学生完成任务，解释平均分。"
            if current["stage_id"] == "b":
                await options["on_delta"]("废弃的当前环节")
                await options["on_reset"]()
                assert "观察（10分钟）" in replay(events, "process")
                assert "废弃" not in replay(events, "process")
            await options["on_delta"](text)
            return Completion(text, {"component_name": "structured"})

    result = await LessonRuntime(SimpleNamespace(ai_provider="mock"), gateway=Gateway()).generate(
        {"topic": "分数", "generation_mode": "interactive"},
        [{"module_id": "process", "module_type": "teaching_process"}], blueprint=BLUEPRINT,
        stage="activities", outline=stages, event_sink=lambda kind, payload: events.append((kind, payload)))
    assert replay(events, "process") == result["modules"][0]["teacher_view_content"]
    assert [payload["stage_index"] for kind, payload in events if kind == "stage_started"] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,module_type", [
    ("one-click", "textbook_analysis"), ("interactive", "textbook_analysis"),
    ("interactive", "teaching_process"),
])
async def test_live_http_delivers_sse_content_while_provider_and_run_are_still_running(client, mode, module_type):
    """Real TCP reads prove the HTTP boundary does not buffer until completion."""
    headers = await login(client)
    released = asyncio.Event()

    class PausedGateway:
        async def complete(self, messages, **options):
            content = options["mock_content"]
            payload = json.loads(messages[-1]["content"])
            if "module" not in payload and "modules" not in payload:
                return Completion(content, {"component_name": "structured"})
            if module_type == "teaching_process":
                first = "[" + json.dumps(json.loads(content)[0], ensure_ascii=False) + ","
                rest = ",".join(json.dumps(s, ensure_ascii=False) for s in json.loads(content)[1:]) + "]"
            else:
                cut = content.index("-->") + 38 if mode == "one-click" else 35
                first, rest = content[:cut], content[cut:]
            await options["on_delta"](first)
            await released.wait()
            await options["on_delta"](rest)
            return Completion(first + rest, {"component_name": "streaming_test"})

    client.app.state.runtime.gateway = PausedGateway()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", access_log=False, log_level="warning"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as live:
                response = await live.post(f"/api/v1/lesson-drafts/generations/{mode}", headers=headers, json={
                    "request_id": "live-sse", "topic": "分数", "school_stage": "primary", "grade": "grade_3",
                    "subject": "math", "lesson_type": "new_lesson", "total_duration_min": 40,
                    "modules": [{"module_type": module_type,
                        "display_name": "教学过程" if module_type == "teaching_process" else "教材分析", "enabled": True}],
                })
                assert response.status_code == 200, response.text
                item = response.json()["data"]
                received_while_running = False
                terminal_count = 0
                async with live.stream("GET", f"/api/v1/generation-runs/{item['run_id']}/events", headers=headers) as stream:
                    assert stream.status_code == 200 and stream.headers["x-accel-buffering"] == "no"
                    kind = ""
                    async for line in stream.aiter_lines():
                        if line.startswith("event: "):
                            kind = line[7:]
                        if kind == "content_delta" and line.startswith("data: ") and not released.is_set():
                            data = json.loads(line[6:])
                            assert data["delta"].strip() and data["is_temporary"]
                            status = (await live.get(f"/api/v1/generation-runs/{item['run_id']}", headers=headers)).json()["data"]
                            assert status["status"] == "running"
                            draft = (await live.get(f"/api/v1/lesson-drafts/{item['draft_id']}", headers=headers)).json()["data"]
                            assert draft["modules"][0]["current_version_id"] is None
                            received_while_running = True
                            released.set()
                        if line == "event: run_completed":
                            terminal_count += 1
                assert received_while_running and terminal_count == 1
    finally:
        released.set()
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        listener.close()
