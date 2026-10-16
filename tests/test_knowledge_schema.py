from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from app.core.config import Settings
from app.db.models import Base, Draft, KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Run, RunCheckpoint
from app.services.knowledge import owned_base, owned_document


def test_knowledge_and_recovery_schema_are_independent_business_facts():
    assert KnowledgeBase.__tablename__ == "knowledge_bases"
    assert KnowledgeDocument.__tablename__ == "knowledge_documents"
    assert KnowledgeChunk.__tablename__ == "knowledge_chunks"
    assert RunCheckpoint.__tablename__ == "ai_run_checkpoints"
    assert {"execution_epoch", "lease_owner", "lease_expires_at", "heartbeat_at", "recovery_count"} <= set(Run.__table__.c.keys())
    assert {"knowledge_base_ids", "knowledge_context_snapshot"} <= set(Draft.__table__.c.keys())


async def test_fresh_schema_contains_indexed_private_chunks_and_checkpoints(client):
    async with client.app.state.db.engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert set(Base.metadata.tables) <= tables
        constraints = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("ai_run_checkpoints"))
        assert any(set(row["column_names"]) == {"run_id", "checkpoint_key"} for row in constraints)


def test_invalid_lease_configuration_does_not_expose_model_credentials():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, worker_lease_seconds=30, worker_heartbeat_seconds=30,
                 llm_api_key="private-model-credential")
    assert "private-model-credential" not in str(error.value)
    assert "input_value=" not in str(error.value)


async def test_postgres_knowledge_lock_modes_allow_fk_checks_without_join_lock_promotion():
    from sqlalchemy.dialects import postgresql

    session = SimpleNamespace(scalar=AsyncMock(return_value=object()))
    teacher = SimpleNamespace(id='teacher', tenant_id='tenant')
    await owned_base(session, 'base', teacher, lock=True)
    sql = str(session.scalar.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert sql.endswith('FOR NO KEY UPDATE')
    await owned_document(session, 'document', teacher, lock=True)
    sql = str(session.scalar.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert sql.endswith('FOR UPDATE OF knowledge_documents')
