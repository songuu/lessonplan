"""Explicit opt-in; standard pytest never contacts external PostgreSQL or Redis."""
import os

import pytest

from scripts.test_external_runtime import endpoints, run_evidence


def test_external_runner_rejects_shared_database(monkeypatch):
    monkeypatch.setenv("LESSON_EXTERNAL_DATABASE_URL", "postgresql+asyncpg://user@127.0.0.1:15432/production")
    monkeypatch.setenv("LESSON_EXTERNAL_REDIS_URL", "redis://127.0.0.1:16379/15")
    with pytest.raises(ValueError, match="lesson_test"):
        endpoints()


@pytest.mark.skipif(not os.environ.get("LESSON_EXTERNAL_DATABASE_URL"), reason="Dedicated PG/Redis evidence run is opt-in")
@pytest.mark.asyncio
async def test_real_postgresql_redis_arq_http_replay():
    evidence = await run_evidence()
    assert evidence["postgresql"]["matches_local_head"] is True
    assert evidence["scenarios"]["running_cancel_late_commit_guard"] is True
    assert evidence["independent_readback"]["runs"] >= 6
    assert evidence["scenarios"]["structured_queue"]["outline_preserved"] is True
    assert evidence["scenarios"]["published_queued_job_loss_recovery"]["stable_job_id_reused"] is True
    assert evidence["scenarios"]["live_sse_pubsub"]["subscriber_connections_released"] is True
    assert evidence["scenarios"]["terminal_sample_denominators"]["cancelled"] == 2
    assert evidence["scenarios"]["terminal_sample_denominators"]["failures"] == 1
    assert evidence["scenarios"]["delete_personal_lesson_during_parse"]["orphan_files"] == 0
