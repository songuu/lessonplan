import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def auth_client(tmp_path):
    from app.api.auth import router
    from app.api.catalog import router as catalog_router
    from app.core.config import Settings
    from app.core.errors import install_error_handlers
    from app.db.models import Base
    from app.db.session import Database

    app = FastAPI()
    app.state.settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}", _env_file=None)
    app.state.db = Database(app.state.settings)
    async with app.state.db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    install_error_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(catalog_router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await app.state.db.engine.dispose()


async def test_register_login_profile_and_logout(auth_client):
    client = auth_client
    payload = {"email": "Teacher@Example.com", "password": "passphrase-123!", "name": "张老师"}
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["user"]["email"] == "teacher@example.com"
    assert "password_hash" not in data["user"]
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 200
    assert (await client.patch("/api/v1/me", json={"name": "李老师", "profile": {"subject": "math"}}, headers=headers)).json()["data"]["name"] == "李老师"
    assert (await client.post("/api/v1/auth/login", json={"email": payload["email"], "password": "wrong-password"})).status_code == 401
    assert (await client.post("/api/v1/auth/login", json=payload)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 401


async def test_duplicate_registration_and_privilege_injection(auth_client):
    payload = {"email": "teacher@example.com", "password": "passphrase-123!", "name": "教师"}
    first = await auth_client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201
    assert (await auth_client.post("/api/v1/auth/register", json=payload)).status_code == 409
    headers = {"Authorization": f"Bearer {first.json()['data']['access_token']}"}
    assert (await auth_client.patch("/api/v1/me", json={"role": "admin"}, headers=headers)).status_code == 422
    assert (await auth_client.get("/api/v1/me")).status_code == 401


async def test_catalog_preferences_are_private_and_validated(auth_client):
    client = auth_client
    registered = await client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "passphrase-123!"})
    headers = {"Authorization": f"Bearer {registered.json()['data']['access_token']}"}
    catalog = (await client.get("/api/v1/lesson-dictionaries", headers=headers)).json()["data"]
    assert len(catalog["modules"]) == 12
    config = (await client.get("/api/v1/me/lesson-module-config", headers=headers)).json()["data"]
    assert len(config["modules"]) == 7
    response = await client.put("/api/v1/me/lesson-module-config", json={"modules": [{"module_type": "custom_module", "display_name": "教学反思", "module_summary": "关注课堂反馈"}]}, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["modules"][0]["module_type"] == "custom_module"
    assert (await client.put("/api/v1/me/lesson-module-config", json={"modules": [{"module_type": "custom_module", "display_name": "教学目标"}]}, headers=headers)).status_code == 422


async def test_tampered_expired_and_disabled_user_tokens_are_rejected(auth_client):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    import jwt

    from app.db.models import User

    registered = await auth_client.post("/api/v1/auth/register", json={"email": "blocked@example.com", "password": "passphrase-123!"})
    data = registered.json()["data"]
    app = auth_client._transport.app
    settings = app.state.settings
    now = datetime.now(UTC)
    expired = jwt.encode({"sub": data["user"]["id"], "jti": str(uuid4()), "iss": settings.jwt_issuer,
                          "iat": now - timedelta(hours=2), "nbf": now - timedelta(hours=2), "exp": now - timedelta(hours=1)}, settings.jwt_secret, algorithm="HS256")
    assert (await auth_client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})).status_code == 401
    token = data["access_token"]
    pieces = token.split(".")
    pieces[1] = pieces[1][:-2] + "ab"
    assert (await auth_client.get("/api/v1/me", headers={"Authorization": "Bearer " + ".".join(pieces)})).status_code == 401
    async with app.state.db.session_factory() as session:
        user = await session.get(User, data["user"]["id"])
        user.is_active = False
        await session.commit()
    assert (await auth_client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
