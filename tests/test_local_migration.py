import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base, Run, User
from scripts.migrate_local_sqlite import copy_database, validate_target


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://teacher@127.0.0.1/lesson_assistant?host=remote.invalid",
    "postgresql+asyncpg://teacher@127.0.0.1/lesson_assistant?database=rag_system",
    "postgresql+asyncpg://teacher@remote.invalid/lesson_assistant",
    "postgresql+asyncpg://teacher@127.0.0.1/rag_system",
])
def test_migration_target_cannot_override_local_database(url):
    with pytest.raises(ValueError, match="local dedicated"):
        validate_target(url)


@pytest.fixture
async def databases(tmp_path):
    engines = [create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}") for name in ["old.db", "new.db"]]
    for engine in engines:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    yield engines
    for engine in engines:
        await engine.dispose()


async def test_copy_preserves_credentials_json_and_dates(databases):
    source, target = databases
    async with source.begin() as conn:
        await conn.execute(User.__table__.insert(), {"id": "teacher-1", "email": "teacher@example.test",
            "password_hash": "existing-password-hash", "name": "迁移教师", "profile": {"note": "保留"}})
    counts = await copy_database(source, target)
    assert counts["users"] == 1
    async with source.connect() as old, target.connect() as new:
        assert (await old.execute(select(User.__table__))).mappings().all() == (
            await new.execute(select(User.__table__))).mappings().all()


async def test_nonempty_target_refused_without_writes(databases):
    source, target = databases
    async with target.begin() as conn:
        await conn.execute(User.__table__.insert(), {"email": "keep@example.test", "password_hash": "keep"})
    with pytest.raises(ValueError, match="Target is not empty"):
        await copy_database(source, target)
    async with target.connect() as conn:
        assert (await conn.execute(select(User.email))).scalars().all() == ["keep@example.test"]


async def test_active_source_run_refused(databases):
    source, target = databases
    async with source.begin() as conn:
        await conn.execute(User.__table__.insert(), {"id": "t", "email": "t@example.test", "password_hash": "hash"})
        await conn.execute(Run.__table__.insert(), {"user_id": "t", "tenant_id": "tenant", "workflow_type": "one_click",
            "idempotency_key": "key", "input_hash": "hash", "status": "running"})
    with pytest.raises(ValueError, match="active generation"):
        await copy_database(source, target)
    async with target.connect() as conn:
        assert (await conn.execute(select(User.id))).all() == []
