import asyncio

import pytest
from sqlalchemy import select

from app.db.models import Event, ModuleVersion, Run
from tests.conftest import login
from tests.test_workflows import INPUT, create, wait_run


@pytest.mark.asyncio
async def test_cancel_rejects_late_provider_commit(client):
    headers = await login(client)
    runtime = client.app.state.runtime
    original = runtime.generate
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        kwargs.pop("event_sink", None)
        return await original(*args, **kwargs)
    runtime.generate = delayed
    item = await create(client, headers)
    await asyncio.wait_for(entered.wait(), 3)
    response = await client.post(f"/api/v1/generation-runs/{item['run_id']}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["data"]["status"] == "cancelled"
    release.set()
    await asyncio.sleep(.2)
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "cancelled"
    async with client.app.state.db.session_factory() as session:
        assert not (await session.scalars(select(ModuleVersion).where(ModuleVersion.draft_id == item["draft_id"]))).all()
        events = (await session.scalars(select(Event).where(Event.run_id == item["run_id"])) ).all()
        assert [e.event_type for e in events if e.event_type in {"run_completed", "run_failed", "run_cancelled"}] == ["run_cancelled"]


@pytest.mark.asyncio
async def test_queue_none_schedules_retry_and_never_claims_success(client):
    headers = await login(client)
    class RejectQueue:
        async def enqueue_job(self, *args, **kwargs):
            return None
        async def get(self, *args):
            return None
        async def aclose(self):
            pass
    client.app.state.settings.task_queue_mode = "arq"
    client.app.state.dispatcher.redis = RejectQueue()
    item = await create(client, headers)
    response = await client.get(f"/api/v1/generation-runs/{item['run_id']}", headers=headers)
    run = response.json()["data"]
    assert run["status"] == "queued" and run["error_code"] is None


@pytest.mark.asyncio
async def test_delete_cancels_and_stream_replay_has_strict_cursor(client):
    headers = await login(client)
    item = await create(client, headers)
    await wait_run(client, headers, item["run_id"])
    replay = await client.get(f"/api/v1/generation-runs/{item['run_id']}/events", headers={**headers, "Last-Event-ID": "2"})
    ids = [int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")]
    assert ids and ids[0] > 2 and ids == sorted(set(ids))
    assert (await client.delete(f"/api/v1/lesson-drafts/{item['draft_id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/lesson-drafts/{item['draft_id']}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_parallel_creation_reuses_run(client):
    headers = await login(client)
    results = await asyncio.gather(*[client.post("/api/v1/lesson-drafts/generations/one-click",
        headers={**headers, "Idempotency-Key": "same"}, json=INPUT) for _ in range(3)])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["data"]["run_id"] for r in results}) == 1
    await wait_run(client, headers, results[0].json()["data"]["run_id"])
    async with client.app.state.db.session_factory() as session:
        assert len((await session.scalars(select(Run).where(Run.workflow_type == "one_click"))).all()) == 1
