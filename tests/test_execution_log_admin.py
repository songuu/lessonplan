"""The admin timeline uses persisted structured data and safe projections only."""

from sqlalchemy import select

from app.db.models import ExecutionLog
from app.services.execution_logs import ExecutionLogWriter
from tests.test_execution_logs import log_payload
from tests.test_execution_recovery import frozen_run
from tests.test_recovery_admin import admin_headers


async def test_execution_admin_read_requires_admin(client):
    teacher, _ = await frozen_run(client)
    for path in ["execution-logs", "observability"]:
        assert (await client.get(f"/api/v1/admin/{path}")).status_code == 401
        assert (await client.get(f"/api/v1/admin/{path}", headers=teacher)).status_code == 403


async def test_execution_log_cursor_filters_and_read_sanitization(client):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    rid = item["run_id"]
    writer = ExecutionLogWriter(client.app.state.db.session_factory)
    for level, status in [("info", "running"), ("error", "failed"), ("warning", "lease_lost")]:
        writer.submit(log_payload(rid, level=level, status=status))
    await writer.start()
    await writer.close()
    async with client.app.state.db.session_factory() as session:
        row = await session.scalar(select(ExecutionLog).where(ExecutionLog.run_id == rid)
            .order_by(ExecutionLog.sequence))
        row.metadata_json = {"total_tokens": 11, "prompt": "PRIVATE_DATABASE_BODY",
            "error_stack": "PRIVATE_STACK", "module_id": {"body": "PRIVATE_NESTED"}}
        await session.commit()
    url = f"/api/v1/admin/execution-logs?run_id={rid}"
    response = await client.get(url + "&limit=2&workflow=one_click", headers=headers)
    assert response.status_code == 200, response.text
    assert "PRIVATE_" not in response.text
    data = response.json()["data"]
    assert len(data["items"]) == 2 and data["has_more"]
    assert data["next_after"] == data["items"][-1]["sequence"]
    assert data["items"][0]["metadata"] == {"total_tokens": 11}
    assert data["items"][0]["workflow_type"] == "one_click"
    next_page = (await client.get(url + f"&after={data['next_after']}&limit=2", headers=headers)).json()["data"]
    assert len(next_page["items"]) == 1 and not next_page["has_more"]
    failed = (await client.get(url + "&level=error&status=failed", headers=headers)).json()["data"]
    assert len(failed["items"]) == 1 and failed["items"][0]["status"] == "failed"
    assert not (await client.get(url + "&workflow=export", headers=headers)).json()["data"]["items"]
    empty = (await client.get(url + "&after=999999", headers=headers)).json()["data"]
    assert empty["items"] == [] and empty["next_after"] == 999999
    for query in ["level=debug", "status=queued", "workflow=bogus", "after=-1", "limit=101", "limit=0"]:
        assert (await client.get(f"/api/v1/admin/execution-logs?{query}", headers=headers)).status_code == 422


async def test_observability_safe_status_and_cloud_disabled_local_history(client, monkeypatch):
    headers = await admin_headers(client)
    monkeypatch.setattr(client.app.state.observer, "status", lambda: {
        "provider": "langfuse", "enabled": False, "configured": True, "project": "test-traces",
        "api_key": "PRIVATE_LANGFUSE_KEY", "error": "PRIVATE_EXCEPTION", "queue_depth": 2})
    monkeypatch.setattr(client.app.state.execution_logs, "status", lambda: {
        "running": True, "queued": 2, "written": 5, "dropped": 1, "api_key": "PRIVATE_LOCAL_KEY"})
    response = await client.get("/api/v1/admin/observability", headers=headers)
    assert response.status_code == 200
    assert "PRIVATE_" not in response.text
    data = response.json()["data"]
    assert data["provider"] == "langfuse" and data["enabled"] is False
    assert data["project"] == "test-traces" and data["local"]["written"] == 5
    assert data["local"]["running"] is True
