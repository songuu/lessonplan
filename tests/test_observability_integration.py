"""Execution diagnostics remain available independently of cloud credentials."""
import pytest
from sqlalchemy import select

from app.db.models import User
from tests.conftest import login
from tests.test_workflows import INPUT, create, wait_run


async def admin(client, headers=None):
    headers = headers or await login(client)
    async with client.app.state.db.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "teacher@example.test"))
        user.role = "admin"
        await session.commit()
    return headers


async def test_generated_lesson_has_durable_execution_steps_without_cloud(client):
    headers = await admin(client)
    item = await create(client, headers)
    run_id = item["run_id"]
    await wait_run(client, headers, run_id)
    if client.app.state.dispatcher.tasks:
        import asyncio
        await asyncio.gather(*list(client.app.state.dispatcher.tasks))
    await client.app.state.execution_logs.flush()
    response = await client.get(f"/api/v1/admin/execution-logs?run_id={run_id}&limit=100", headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    rows = page["items"]
    while page["has_more"]:
        page = (await client.get(f"/api/v1/admin/execution-logs?run_id={run_id}&limit=100&after={page['next_after']}",
                                 headers=headers)).json()["data"]
        rows.extend(page["items"])
    assert any(row["name"] == "lesson.execute" and row["phase"] == "end"
               and row["status"] == "completed" for row in rows)
    assert any(row["name"] == "runtime.generate" for row in rows)
    assert not any(row["run_type"] == "llm" for row in rows), "mock content is not a paid Provider request"
    assert len({row["trace_id"] for row in rows}) == 1
    assert INPUT["topic"] not in response.text


async def test_observability_configuration_is_admin_only_and_local_logs_enabled(client):
    headers = await login(client)
    assert (await client.get("/api/v1/admin/observability", headers=headers)).status_code == 403
    headers = await admin(client, headers)
    response = await client.get("/api/v1/admin/observability", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is False
    assert "api_key" not in response.text.lower()


async def test_uncommitted_execution_cannot_be_reported_as_success(client, monkeypatch):
    from app.services.lessons import WorkflowExecutor
    from tests.test_execution_recovery import frozen_run

    headers, item = await frozen_run(client)
    await admin(client, headers)

    async def interrupted(*args, **kwargs):
        raise OSError("PRIVATE_SYSTEM_FAILURE")

    monkeypatch.setattr(WorkflowExecutor, "execute", interrupted)
    with pytest.raises(OSError):
        await client.app.state.dispatcher._execute(item["run_id"])
    await client.app.state.execution_logs.flush()
    response = await client.get(f"/api/v1/admin/execution-logs?run_id={item['run_id']}&status=interrupted",
                                headers=headers)
    assert response.status_code == 200
    roots = [row for row in response.json()["data"]["items"] if row["name"] == "lesson.execute"]
    assert len(roots) == 1 and roots[0]["status"] == "interrupted"
    assert "PRIVATE_SYSTEM_FAILURE" not in response.text
