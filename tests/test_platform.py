import pytest
from pydantic import ValidationError


def test_production_requires_real_dependencies_and_strong_key():
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(environment="production", _env_file=None)


def test_production_rejects_default_or_short_secret():
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            environment="production", database_url="postgresql+asyncpg://u:p@localhost/lesson",
            task_queue_mode="arq", llm_provider="openai", llm_api_key="configured",
            llm_base_url="https://example.com/v1", jwt_secret="short", _env_file=None,
        )


def test_module_validation_canonicalizes_optional_and_rejects_duplicates():
    from app.core.catalog import normalize_modules
    from app.core.errors import AppError

    result = normalize_modules([{"display_name": "作业设计", "module_type": "custom_module"}])
    assert result[0]["module_type"] == "homework_design"
    assert result[0]["module_id"]
    with pytest.raises(AppError):
        normalize_modules([{"display_name": "教学目标", "module_type": "custom_module"}])
    with pytest.raises(AppError):
        normalize_modules([{"module_type": "teaching_objectives"}, {"module_type": "teaching_objectives"}])


async def test_database_schema_and_active_run_constraints(tmp_path):
    from sqlalchemy.exc import IntegrityError

    from app.core.config import Settings
    from app.db.models import Base, Draft, Run, User
    from app.db.session import Database

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'platform.db'}", _env_file=None))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with db.session_factory() as session:
        user = User(email="schema@example.com", password_hash="not-used", name="教师")
        session.add(user)
        await session.flush()
        draft = Draft(user_id=user.id, tenant_id=user.tenant_id, request_id="schema", input_hash="hash", topic="分数")
        session.add(draft)
        await session.flush()
        session.add(Run(draft_id=draft.id, user_id=user.id, tenant_id=user.tenant_id, workflow_type="one_click", idempotency_key="key-a", input_hash="hash"))
        await session.commit()
        session.add(Run(draft_id=draft.id, user_id=user.id, tenant_id=user.tenant_id, workflow_type="interactive_step", idempotency_key="key-b", input_hash="hash"))
        with pytest.raises(IntegrityError):
            await session.commit()
    await db.engine.dispose()


async def test_utc_roundtrip_and_terminal_event_constraint(tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from app.core.config import Settings
    from app.db.models import Base, Event, Run, User
    from app.db.session import Database

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'terminal.db'}", _env_file=None))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with db.session_factory() as session:
        user = User(email="terminal@example.com", password_hash="not-used")
        session.add(user)
        await session.flush()
        run = Run(user_id=user.id, tenant_id=user.tenant_id, workflow_type="export", idempotency_key="terminal", input_hash="hash")
        session.add(run)
        await session.flush()
        session.add(Event(run_id=run.id, sequence=1, event_type="run_completed"))
        await session.commit()
        await session.refresh(user)
        assert user.created_at.tzinfo == UTC
        assert datetime.now(UTC) >= user.created_at
        session.add(Event(run_id=run.id, sequence=2, event_type="run_failed"))
        with pytest.raises(IntegrityError):
            await session.commit()
    await db.engine.dispose()


def test_initial_migration_upgrade_check_and_downgrade(tmp_path):
    import os
    import subprocess
    import sys

    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}", "ENVIRONMENT": "test"}
    for arguments in [("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")]:
        result = subprocess.run([sys.executable, "-m", "alembic", *arguments], env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_postgres_migration_sql_compiles(tmp_path):
    import os
    import subprocess
    import sys

    environment = {**os.environ, "DATABASE_URL": "postgresql+asyncpg://compile:compile@localhost/compile", "ENVIRONMENT": "test"}
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head", "--sql"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "CREATE UNIQUE INDEX uq_active_foreground_run" in result.stdout
    assert "WHERE status IN ('queued', 'running')" in result.stdout
