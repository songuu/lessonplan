"""请求和 Worker 共享同一连接配置，不共享 AsyncSession。"""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


class Database:
    def __init__(self, settings: Settings):
        options = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            options["connect_args"] = {"timeout": 30}
        self.engine = create_async_engine(settings.database_url, **options)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        if settings.database_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
