"""Recovery operators see receipt facts, never private model response bodies."""
from sqlalchemy import select

from app.ai.gateway import Completion
from app.db.models import RecoveryAlert, User
from app.services.invocations import InvocationJournal
from app.services.lessons import WorkflowExecutor
from tests.test_execution_recovery import frozen_run


async def test_receipt_projection_is_admin_only_and_excludes_raw_response(client):
    headers, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    assert await executor.claim(item["run_id"], 0)
    journal = InvocationJournal(executor, item["run_id"], "safe-fingerprint")
    claim = await journal.reserve("polish", 1, "a" * 64, "call-1", 1, [], "module_operation")
    await journal.save_response(claim["id"], Completion("PRIVATE_RAW_MODEL_RESPONSE", {
        "model_name": "model-test", "llm_provider": "openai_compatible", "call_outcome": "success",
        "provider_request_id": "provider-request-123", "prompt_tokens": 12,
        "completion_tokens": 8, "total_tokens": 20, "secret": "NEVER_EXPOSE"}))
    url = f"/api/v1/admin/recovery-runs/{item['run_id']}"
    assert (await client.get(url, headers=headers)).status_code == 403
    async with client.app.state.db.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "teacher@example.test"))
        user.role = "admin"
        await session.commit()
    response = await client.get(url, headers=headers)
    assert response.status_code == 200
    rows = response.json()["data"]["invocations"]
    assert len(rows) == 1 and rows[0]["status"] == "response_saved"
    assert rows[0]["total_tokens"] == 20 and rows[0]["provider_request_id"] == "provider-request-123"
    assert rows[0]["response_hash"] and rows[0]["capture_epoch"] == 0
    assert "PRIVATE_RAW_MODEL_RESPONSE" not in response.text and "NEVER_EXPOSE" not in response.text
    assert "response_body" not in rows[0] and "capture_owner" not in rows[0]


async def test_unknown_closes_automatic_generation_with_persistent_alert(client):
    headers, item = await frozen_run(client)
    executor = WorkflowExecutor(client.app)
    assert await executor.claim(item["run_id"], 0)
    journal = InvocationJournal(executor, item["run_id"], "safe-fingerprint")
    claim = await journal.reserve("batch:direct", 1, "a" * 64, "call-1", 1, [], "one_click")
    await journal.mark_unknown(claim["id"], "LLM_OUTCOME_UNKNOWN")
    await executor.fail(item["run_id"], "LLM_OUTCOME_UNKNOWN", "模型调用结果未知，已停止自动重发")
    response = await client.get(f"/api/v1/generation-runs/{item['run_id']}", headers=headers)
    assert response.json()["data"]["status"] == "failed"
    assert response.json()["data"]["error_code"] == "LLM_OUTCOME_UNKNOWN"
    async with client.app.state.db.session_factory() as session:
        alert = await session.scalar(select(RecoveryAlert).where(RecoveryAlert.run_id == item["run_id"],
            RecoveryAlert.code == "LLM_OUTCOME_UNKNOWN"))
        assert alert is not None and alert.status == "open"
    assert not await client.app.state.dispatcher.recover_run(item["run_id"], expected_epoch=0)
