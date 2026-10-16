import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db.models import Base
from app.factory import create_app


@pytest_asyncio.fixture
async def client(tmp_path):
    settings = Settings(
        _env_file=None, environment="test", storage_backend="local",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        storage_root=str(tmp_path / 'storage'),
        task_queue_mode="inline",
        ai_provider="mock",
        jwt_secret="test-only-secret-of-sufficient-length-123456789",
    )
    assert settings.ai_provider == "mock" and settings.storage_backend == "local"
    app = create_app(settings)
    async with app.state.db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            http.app = app
            yield http


async def login(client, email="teacher@example.test"):
    response = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "Test!Password123", "name": "林老师",
    })
    assert response.status_code in (200, 201), response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}
