import asyncio

import pytest

from tests.conftest import login

INPUT = {"request_id": "create-1", "topic": "分数的初步认识", "school_stage": "primary",
         "grade": "三年级", "subject": "数学", "lesson_type": "new_lesson", "total_duration_min": 40}


async def wait_run(client, headers, run_id):
    for _ in range(200):
        data = (await client.get(f"/api/v1/generation-runs/{run_id}", headers=headers)).json()["data"]
        if data["status"] in {"completed", "failed", "cancelled"}:
            return data
        await asyncio.sleep(0.02)
    pytest.fail("运行未收口")


async def create(client, headers, mode="one-click", key="first", payload=None):
    response = await client.post(f"/api/v1/lesson-drafts/generations/{mode}",
                                 headers={**headers, "Idempotency-Key": key}, json=payload or INPUT)
    assert response.status_code in (200, 201, 202), response.text
    return response.json()["data"]


def lock_fields(draft, module):
    return {"expected_content_version": draft["content_version"],
            "expected_current_module_version_id": module["current_version_id"],
            "expected_module_config_version_id": draft["module_config_version_id"]}


@pytest.mark.asyncio
async def test_one_click_replay_idempotency_and_ownership(client):
    headers = await login(client)
    item = await create(client, headers)
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
    same = await create(client, headers)
    assert same == item
    conflict = await client.post("/api/v1/lesson-drafts/generations/one-click",
        headers={**headers, "Idempotency-Key": "first"}, json={**INPUT, "topic": "三角形"})
    assert conflict.status_code == 409
    detail = (await client.get(f"/api/v1/lesson-drafts/{item['draft_id']}", headers=headers)).json()["data"]
    assert detail["status"] == "ready" and detail["hard_gate_status"] == "passed"
    assert len(detail["modules"]) == 7
    replay = await client.get(f"/api/v1/generation-runs/{item['run_id']}/events", headers=headers)
    assert replay.text.count("event: run_completed") == 1
    assert "event: draft_ready" in replay.text
    other = await login(client, "other@example.test")
    assert (await client.get(f"/api/v1/lesson-drafts/{item['draft_id']}", headers=other)).status_code == 404
    assert (await client.get(f"/api/v1/generation-runs/{item['run_id']}", headers=other)).status_code == 404


@pytest.mark.asyncio
async def test_edit_conflict_history_restore_and_candidate_isolation(client):
    headers = await login(client)
    item = await create(client, headers)
    await wait_run(client, headers, item["run_id"])
    path = f"/api/v1/lesson-drafts/{item['draft_id']}"
    draft = (await client.get(path, headers=headers)).json()["data"]
    module = draft["modules"][0]
    body = {**lock_fields(draft, module), "teacher_view_content": "教师手工保存的教材分析，基于分数的初步认识。"}
    updated = await client.patch(f"{path}/modules/{module['module_id']}", headers=headers, json=body)
    assert updated.status_code == 200, updated.text
    assert (await client.patch(f"{path}/modules/{module['module_id']}", headers=headers, json=body)).status_code == 409
    session = (await client.post("/api/v1/module-polish-sessions", headers=headers,
        json={"draft_id": item["draft_id"], "module_id": module["module_id"], "scope": "module"})).json()["data"]
    message = await client.post(f"/api/v1/module-polish-sessions/{session['id']}/messages", headers=headers,
        json={"instruction": "更简洁", "working_content": body["teacher_view_content"]})
    assert message.status_code == 200, message.text
    await wait_run(client, headers, message.json()["data"]["run_id"])
    after = (await client.get(path, headers=headers)).json()["data"]
    assert after["modules"][0]["teacher_view_content"] == body["teacher_view_content"]
    history = (await client.get(f"{path}/modules/{module['module_id']}/versions", headers=headers)).json()["data"]
    assert len(history) == 2
    restored = await client.post(f"{path}/modules/{module['module_id']}/restore", headers=headers,
        json={**lock_fields(after, after["modules"][0]), "version_id": history[-1]["id"]})
    assert restored.status_code == 200, restored.text


@pytest.mark.asyncio
async def test_export_and_reflection_separation(client):
    headers = await login(client)
    item = await create(client, headers)
    await wait_run(client, headers, item["run_id"])
    path = f"/api/v1/lesson-drafts/{item['draft_id']}"
    preview = await client.post(f"{path}/preview", headers=headers)
    assert preview.status_code == 200 and preview.json()["data"]["content"]
    for fmt, signature in [("docx", b"PK"), ("pdf", b"%PDF")]:
        response = await client.post(f"{path}/exports", headers=headers, json={"format": fmt})
        assert response.status_code == 200, response.text
        export = response.json()["data"]
        await wait_run(client, headers, export["run_id"])
        download = await client.get(f"/api/v1/lesson-exports/{export['id']}/download", headers=headers)
        assert download.status_code == 200 and download.content.startswith(signature)
    before = (await client.get(path, headers=headers)).json()["data"]["content_version"]
    assert (await client.put(f"{path}/post-lesson-reflection", headers=headers,
        json={"content": "学生在折纸活动中理解了平均分。", "expected_version": 0})).status_code == 200
    assert (await client.get(path, headers=headers)).json()["data"]["content_version"] == before


@pytest.mark.asyncio
async def test_interactive_requires_confirmation_and_process_activities(client):
    headers = await login(client)
    payload = {**INPUT, "request_id": "interactive", "modules": [
        {"module_type": "teaching_objectives", "display_name": "教学目标", "enabled": True},
        {"module_type": "teaching_process", "display_name": "教学过程", "enabled": True}]}
    item = await create(client, headers, "interactive", payload=payload)
    await wait_run(client, headers, item["run_id"])
    path = f"/api/v1/lesson-drafts/{item['draft_id']}"
    assert (await client.post(f"{path}/interactive/finalize", headers=headers, json={})).status_code == 409
    draft = (await client.get(path, headers=headers)).json()["data"]
    module = draft["modules"][0]
    assert (await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": module["module_id"], **lock_fields(draft, module)})).status_code == 200
    process = draft["modules"][1]
    step = await client.post(f"{path}/interactive/step", headers=headers, json={"module_id": process["module_id"], "stage": "outline"})
    await wait_run(client, headers, step.json()["data"]["run_id"])
    draft = (await client.get(path, headers=headers)).json()["data"]
    process = draft["modules"][1]
    outline = [{"stage_id": "teacher-stage", "title": "教师确认的探究", "duration_min": 17, "objective": "观察分数"}]
    confirm = await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": process["module_id"], **lock_fields(draft, process), "outline": outline})
    assert confirm.status_code == 200, confirm.text
    assert (await client.post(f"{path}/interactive/finalize", headers=headers, json={})).status_code == 409
    step = await client.post(f"{path}/interactive/step", headers=headers, json={"module_id": process["module_id"], "stage": "activities"})
    await wait_run(client, headers, step.json()["data"]["run_id"])
    draft = (await client.get(path, headers=headers)).json()["data"]
    process = draft["modules"][1]
    assert "教师确认的探究" in process["teacher_view_content"]
    assert (await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": process["module_id"], **lock_fields(draft, process)})).status_code == 200
    done = await client.post(f"{path}/interactive/finalize", headers=headers, json={})
    assert done.status_code == 200, done.text
    assert (await client.get(path, headers=headers)).json()["data"]["status"] == "ready"
