"""Recovery administration must preserve execution ownership and redact generation data."""
from datetime import timedelta

from sqlalchemy import select

from app.db.models import Audit, Event, Outbox, Run, RunCheckpoint, User
from app.services.lessons import WorkflowExecutor, database_now
from tests.conftest import login
from tests.test_execution_recovery import expire, frozen_run


async def admin_headers(client):
    headers = await login(client, "recovery-admin@example.test")
    async with client.app.state.db.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "recovery-admin@example.test"))
        user.role = "admin"
        await session.commit()
    return headers


async def test_recovery_management_requires_admin_for_reads_and_writes(client):
    teacher, item = await frozen_run(client)
    rid = item["run_id"]
    for path in ["recovery-overview", "recovery-runs", f"recovery-runs/{rid}", "recovery-alerts"]:
        assert (await client.get(f"/api/v1/admin/{path}", headers=teacher)).status_code == 403
    for path, body in [
        (f"recovery-runs/{rid}/retry", {"expected_epoch": 0, "expected_attempts": 0, "reason": "检查投递状态"}),
        (f"recovery-runs/{rid}/cancel", {"expected_epoch": 0, "reason": "停止演示任务"}),
        ("recovery-alerts/missing/acknowledge", {"reason": "检查演示告警"}),
        ("recovery-alerts/missing/resolve", {"reason": "检查演示告警"}),
    ]:
        assert (await client.post(f"/api/v1/admin/{path}", headers=teacher, json=body)).status_code == 403


async def test_recovery_list_validates_filters_and_paginates_actual_records(client):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    response = await client.get("/api/v1/admin/recovery-runs?status=queued&limit=1&search=分数", headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["total"] == 1 and len(page["items"]) == 1
    row = page["items"][0]
    assert row["id"] == item["run_id"] and row["dispatch"]["attempts"] == 0
    assert row["can_retry"] and row["can_cancel"] and page["server_time"]
    empty = (await client.get("/api/v1/admin/recovery-runs?offset=1&limit=1", headers=headers)).json()["data"]
    assert empty["total"] == 1 and empty["items"] == []
    for query in ["limit=101", "offset=-1", "status=bogus"]:
        assert (await client.get(f"/api/v1/admin/recovery-runs?{query}", headers=headers)).status_code == 422
    overview = (await client.get("/api/v1/admin/recovery-overview", headers=headers)).json()["data"]
    assert overview["queued"] == 1 and overview["retry_pending"] == 1


async def test_recovery_details_use_bounded_safe_projections_and_live_budget(client):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    rid = item["run_id"]
    secret = "PRIVATE_PROVIDER_SECRET_DO_NOT_RETURN"
    async with client.app.state.db.session_factory() as session:
        session.add(RunCheckpoint(run_id=rid, checkpoint_key="budget", input_fingerprint="fp",
            payload={"calls_started": 2, "attempt_counts": {"module:sample": 1}, "calls": [
                {"call_id": "call-1", "model_name": "test-model", "call_outcome": "interrupted", "api_key": secret},
                {"call_id": "call-2", "call_outcome": "started", "messages": secret}], "value": {"secret": secret}}))
        session.add(RunCheckpoint(run_id=rid, checkpoint_key="module:sample", input_fingerprint="fp",
            payload={"value": {"teacher_view_content": secret}}))
        session.add(Event(run_id=rid, sequence=1, event_type="content_delta", payload={"delta": secret}))
        session.add(Event(run_id=rid, sequence=2, event_type="module_progress",
            payload={"phase": "worker_recovering", "execution_epoch": 1, "secret": secret}))
        run = await session.get(Run, rid)
        run.sequence = 2
        await session.commit()
    response = await client.get(f"/api/v1/admin/recovery-runs/{rid}?limit=1", headers=headers)
    assert response.status_code == 200, response.text
    assert secret not in response.text
    detail = response.json()["data"]
    assert detail["event_total"] == 2 and len(detail["events"]) == 1
    assert detail["budget"]["calls_started"] == 2 and detail["budget"]["max_calls"] == 32
    assert len(detail["checkpoints"]) == 2 and detail["checkpoints"][0]["content_hash"]
    assert [call["call_outcome"] for call in detail["calls"]] == ["interrupted", "started"]
    next_page = (await client.get(f"/api/v1/admin/recovery-runs/{rid}?after=1", headers=headers)).json()["data"]
    assert [event["sequence"] for event in next_page["events"]] == [2]


async def test_admin_retry_keeps_budget_and_rejects_stale_attempt(client, monkeypatch):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    rid = item["run_id"]
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == rid))
        intent.attempts = 2
        intent.available_at = await database_now(session) + timedelta(minutes=1)
        intent.dispatch_deadline_at = await database_now(session) + timedelta(minutes=3)
        deadline = intent.dispatch_deadline_at
        await session.commit()
    submitted = []
    async def submit(run_id, **kwargs):
        submitted.append((run_id, kwargs))
    monkeypatch.setattr(client.app.state.dispatcher, "submit", submit)
    body = {"expected_epoch": 0, "expected_attempts": 1, "reason": "确认队列恢复后重投"}
    assert (await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)).status_code == 409
    body["expected_attempts"] = 2
    result = await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert submitted == [(rid, {"recover": True})]
    async with client.app.state.db.session_factory() as session:
        intent = await session.scalar(select(Outbox).where(Outbox.run_id == rid))
        assert intent.attempts == 2 and intent.dispatch_deadline_at == deadline
        assert intent.available_at <= await database_now(session)
        audit = await session.scalar(select(Audit).where(Audit.action == "recovery.retry"))
        assert audit.resource_id == rid and audit.metadata_json["reason"] == body["reason"]


async def test_admin_cannot_steal_live_lease_or_revive_terminal_run(client):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    rid = item["run_id"]
    owner = WorkflowExecutor(client.app)
    assert await owner.claim(rid)
    body = {"expected_epoch": 0, "expected_attempts": 0, "reason": "检查有效任务保护"}
    assert (await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)).status_code == 409
    cancel = await client.post(f"/api/v1/admin/recovery-runs/{rid}/cancel", headers=headers,
        json={"expected_epoch": 0, "reason": "取消此测试生成任务"})
    assert cancel.status_code == 200 and cancel.json()["data"]["status"] == "cancelled"
    assert not await owner.renew_lease()
    assert (await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)).status_code == 409
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, rid)
        assert run.cancel_requested and run.execution_epoch == 0
        assert await session.scalar(select(Audit).where(Audit.action == "recovery.cancel"))


async def test_admin_can_recover_only_expired_epoch_and_records_audit(client, monkeypatch):
    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    rid = item["run_id"]
    owner = WorkflowExecutor(client.app)
    assert await owner.claim(rid)
    await expire(client, rid)
    submitted = []
    async def submit(run_id, **kwargs):
        submitted.append(run_id)
    monkeypatch.setattr(client.app.state.dispatcher, "submit", submit)
    body = {"expected_epoch": 9, "expected_attempts": 0, "reason": "接管已过期任务"}
    assert (await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)).status_code == 409
    body["expected_epoch"] = 0
    response = await client.post(f"/api/v1/admin/recovery-runs/{rid}/retry", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["execution_epoch"] == 1 and submitted == [rid]


async def test_alert_acknowledgement_and_resolution_are_audited_without_metadata_leaks(client):
    from app.db.models import RecoveryAlert
    from app.services.recovery import signal_alert

    _, item = await frozen_run(client)
    headers = await admin_headers(client)
    async with client.app.state.db.session_factory() as session:
        run = await session.get(Run, item["run_id"])
        alert = await signal_alert(session, "DISPATCH_RETRY", "队列投递等待重试", run=run,
            metadata={"attempts": 1, "api_key": "DO_NOT_EXPOSE_METADATA"})
        await session.commit()
        alert_id = alert.id
    response = await client.get("/api/v1/admin/recovery-alerts?status=open", headers=headers)
    assert response.status_code == 200 and "DO_NOT_EXPOSE_METADATA" not in response.text
    assert response.json()["data"]["total"] == 1
    for action, status in [("acknowledge", "acknowledged"), ("resolve", "resolved")]:
        response = await client.post(f"/api/v1/admin/recovery-alerts/{alert_id}/{action}", headers=headers,
            json={"reason": "已检查并记录恢复处置"})
        assert response.status_code == 200 and response.json()["data"]["status"] == status
    async with client.app.state.db.session_factory() as session:
        alert = await session.get(RecoveryAlert, alert_id)
        assert alert.acknowledged_at and alert.acknowledged_by and alert.resolved_at
        records = (await session.scalars(select(Audit).where(Audit.resource_id == alert_id))).all()
        assert {record.action for record in records} == {"recovery.alert.acknowledge", "recovery.alert.resolve"}
