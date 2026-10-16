import asyncio

import pytest

from tests.conftest import login
from tests.test_workflows import INPUT, create, lock_fields, wait_run


@pytest.mark.asyncio
async def test_export_cancel_closes_record_and_cross_workflow_key_conflicts(client):
    headers = await login(client)
    item = await create(client, headers)
    await wait_run(client, headers, item["run_id"])
    original_submit = client.app.state.dispatcher.submit
    async def frozen(run_id):
        pass
    client.app.state.dispatcher.submit = frozen
    exported = (await client.post(f"/api/v1/lesson-drafts/{item['draft_id']}/exports",
        headers={**headers, "Idempotency-Key": "export-key"}, json={"format": "docx"})).json()["data"]
    conflict = await client.post("/api/v1/lesson-drafts/generations/one-click",
        headers={**headers, "Idempotency-Key": "export-key"}, json=INPUT)
    assert conflict.status_code == 409
    same = (await client.post(f"/api/v1/lesson-drafts/{item['draft_id']}/exports",
        headers={**headers, "Idempotency-Key": "export-key"}, json={"format": "docx"})).json()["data"]
    assert same["id"] == exported["id"]
    assert (await client.post(f"/api/v1/generation-runs/{exported['run_id']}/cancel", headers=headers)).status_code == 200
    record = (await client.get(f"/api/v1/lesson-exports/{exported['id']}", headers=headers)).json()["data"]
    assert record["status"] == "failed" and "取消" in record["error_message"]
    client.app.state.dispatcher.submit = original_submit


@pytest.mark.asyncio
async def test_manual_dangerous_formula_is_controlled_error(client):
    headers = await login(client)
    item = await create(client, headers)
    await wait_run(client, headers, item["run_id"])
    path = f"/api/v1/lesson-drafts/{item['draft_id']}"
    draft = (await client.get(path, headers=headers)).json()["data"]
    module = draft["modules"][0]
    bad = await client.patch(f"{path}/modules/{module['module_id']}", headers=headers,
        json={**lock_fields(draft, module), "teacher_view_content": r"分数 \(\input{secret}\)"})
    assert bad.status_code == 422
    assert (await client.get(path, headers=headers)).json()["data"]["content_version"] == draft["content_version"]


@pytest.mark.asyncio
async def test_bad_stage_dictionary_and_empty_topic_are_rejected(client):
    headers = await login(client)
    for patch in [{"school_stage": "unknown"}, {"grade": "高三"}, {"topic": "  "}]:
        result = await client.post("/api/v1/lesson-drafts/generations/one-click", headers=headers, json={**INPUT, **patch})
        assert result.status_code == 422, result.text


@pytest.mark.asyncio
async def test_timeout_is_failed_not_user_cancelled(client):
    headers = await login(client)
    client.app.state.settings.run_timeout_seconds = 0.1
    async def slow(*args, **kwargs):
        await asyncio.sleep(2)
    client.app.state.runtime.generate = slow
    item = await create(client, headers)
    run = await wait_run(client, headers, item["run_id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKFLOW_TIMEOUT"
