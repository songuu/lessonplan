"""Interactive navigation never grants a second foreground task admission."""

import pytest
from sqlalchemy import func, select

from app.db.models import PolishMessage, PolishSession, Run
from tests.conftest import login
from tests.test_workflows import INPUT, create, lock_fields, wait_run


async def prepared_draft(client, headers):
    item = await create(client, headers, "interactive", payload={
        **INPUT, "request_id": "module-admission", "modules": [
            {"module_type": "textbook_analysis", "display_name": "教材分析", "enabled": True},
            {"module_type": "teaching_objectives", "display_name": "教学目标", "enabled": True},
        ],
    })
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
    path = f"/api/v1/lesson-drafts/{item['draft_id']}"
    draft = (await client.get(path, headers=headers)).json()["data"]
    first = draft["modules"][0]
    confirmed = await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": first["module_id"], **lock_fields(draft, first)})
    assert confirmed.status_code == 200, confirmed.text
    return path, confirmed.json()["data"]


async def set_run_status(client, run_id, status):
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, run_id)
        run.status = status
        await session.commit()


async def row_counts(client):
    async with client.app.state.db.session_factory() as session:
        return tuple([await session.scalar(select(func.count()).select_from(model))
            for model in (Run, PolishSession, PolishMessage)])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_active_module_task_allows_readback_but_rejects_other_generation_and_polish(
        client, monkeypatch, status):
    headers = await login(client)
    path, draft = await prepared_draft(client, headers)
    first, second = draft["modules"]
    polish_payload = {"draft_id": draft["id"], "module_id": first["module_id"], "scope": "module"}
    opened = await client.post("/api/v1/module-polish-sessions", headers=headers, json=polish_payload)
    assert opened.status_code == 200, opened.text
    session_id = opened.json()["data"]["id"]
    message_path = f"/api/v1/module-polish-sessions/{session_id}/messages"
    message_payload = {"instruction": "让表述更简洁", "working_content": first["teacher_view_content"]}

    async def hold_dispatch(_run_id):
        # Stop at admission so queued/running checks have no model timing dependency.
        return None

    monkeypatch.setattr(client.app.state.dispatcher, "submit", hold_dispatch)
    step_headers = {**headers, "Idempotency-Key": "second-module"}
    started = await client.post(f"{path}/interactive/step", headers=step_headers,
        json={"module_id": second["module_id"]})
    assert started.status_code == 200, started.text
    active_id = started.json()["data"]["run_id"]
    await set_run_status(client, active_id, status)
    before = await row_counts(client)

    # Revisiting committed content is a read, including its history and polish session.
    viewed = await client.get(path, headers=headers)
    assert viewed.status_code == 200
    assert viewed.json()["data"]["active_run_id"] == active_id
    assert viewed.json()["data"]["modules"][0]["teacher_view_content"] == first["teacher_view_content"]
    assert (await client.get(f"{path}/modules/{first['module_id']}/versions", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/module-polish-sessions/{session_id}", headers=headers)).status_code == 200

    blocked = [
        await client.post(f"{path}/interactive/step", headers=headers, json={"module_id": first["module_id"]}),
        await client.post("/api/v1/module-polish-sessions", headers=headers, json=polish_payload),
        await client.post(message_path, headers=headers, json=message_payload),
    ]
    assert [(response.status_code, response.json().get("error", {}).get("code")) for response in blocked] == [
        (409, "RUN_ACTIVE"), (409, "RUN_ACTIVE"), (409, "RUN_ACTIVE")]
    assert await row_counts(client) == before
    replay = await client.post(f"{path}/interactive/step", headers=step_headers,
        json={"module_id": second["module_id"]})
    assert replay.status_code == 200 and replay.json()["data"]["run_id"] == active_id

    # Terminal state releases admission; identical active requests remain safe replays.
    await set_run_status(client, active_id, "completed")
    reopened = await client.post("/api/v1/module-polish-sessions", headers=headers, json=polish_payload)
    assert reopened.status_code == 200, reopened.text
    sent = await client.post(message_path, headers={**headers, "Idempotency-Key": "polish-once"}, json=message_payload)
    assert sent.status_code == 200, sent.text
    sent_again = await client.post(message_path, headers={**headers, "Idempotency-Key": "polish-once"}, json=message_payload)
    assert sent_again.status_code == 200 and sent_again.json() == sent.json()
    polish_busy = [
        await client.post(f"{path}/interactive/step", headers=headers, json={"module_id": first["module_id"]}),
        await client.post("/api/v1/module-polish-sessions", headers=headers, json=polish_payload),
        await client.post(message_path, headers=headers, json=message_payload),
    ]
    assert [(response.status_code, response.json()["error"]["code"]) for response in polish_busy] == [
        (409, "RUN_ACTIVE"), (409, "RUN_ACTIVE"), (409, "RUN_ACTIVE")]
    await set_run_status(client, sent.json()["data"]["run_id"], "completed")
    regenerated = await client.post(f"{path}/interactive/step", headers=headers,
        json={"module_id": first["module_id"]})
    assert regenerated.status_code == 200, regenerated.text


@pytest.mark.asyncio
async def test_confirm_cannot_skip_an_earlier_regenerated_module(client):
    headers = await login(client)
    path, draft = await prepared_draft(client, headers)
    first, second = draft["modules"]
    generated = await client.post(f"{path}/interactive/step", headers=headers,
        json={"module_id": second["module_id"]})
    assert generated.status_code == 200, generated.text
    assert (await wait_run(client, headers, generated.json()["data"]["run_id"]))["status"] == "completed"
    regenerated = await client.post(f"{path}/interactive/step", headers=headers,
        json={"module_id": first["module_id"]})
    assert regenerated.status_code == 200, regenerated.text
    assert (await wait_run(client, headers, regenerated.json()["data"]["run_id"]))["status"] == "completed"
    draft = (await client.get(path, headers=headers)).json()["data"]
    second = draft["modules"][1]
    skipped = await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": second["module_id"], **lock_fields(draft, second)})
    assert skipped.status_code == 409, skipped.text
    assert skipped.json()["error"]["code"] == "PREVIOUS_MODULE_UNCONFIRMED"
    first = draft["modules"][0]
    confirmed = await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": first["module_id"], **lock_fields(draft, first)})
    assert confirmed.status_code == 200, confirmed.text
    draft = confirmed.json()["data"]
    second = draft["modules"][1]
    resumed = await client.post(f"{path}/interactive/confirm", headers=headers,
        json={"module_id": second["module_id"], **lock_fields(draft, second)})
    assert resumed.status_code == 200, resumed.text

