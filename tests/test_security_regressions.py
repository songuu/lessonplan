import pytest
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError
from starlette.datastructures import UploadFile

from app.ai.quality import SafePatchProcessor
from app.core.config import Settings
from app.db.models import Issue
from app.integrations.documents import DocumentAdapter
from app.services.lessons import WorkflowExecutor
from scripts.test_external_runtime import endpoints
from tests.conftest import login
from tests.test_workflows import create, wait_run


@pytest.mark.asyncio
async def test_anonymous_upload_never_parses_and_chunked_oversize_never_spools(client, monkeypatch):
    headers = await login(client)
    client.app.state.settings.upload_max_bytes = 1024
    written = []
    original = UploadFile.write
    async def write_spy(self, data):
        written.append(len(data))
        return await original(self, data)
    monkeypatch.setattr(UploadFile, "write", write_spy)
    sent = []
    async def body():
        for _ in range(1024):
            sent.append(2048)
            yield b"x" * 2048
    media = {"Content-Type": "multipart/form-data; boundary=test", "Origin": "http://localhost:5173"}
    anonymous = await client.post("/api/v1/personal-lessons", headers=media, content=body())
    assert anonymous.status_code == 401
    assert anonymous.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert not sent and not written
    oversized = await client.post("/api/v1/personal-lessons", headers={**headers, **media}, content=body())
    assert oversized.status_code == 413 and not written
    assert oversized.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert sum(sent) <= 1024 + 8192 + 2048


@pytest.mark.asyncio
async def test_orm_error_with_none_code_still_closes_run(client):
    headers = await login(client)
    async def broken(*args, **kwargs):
        raise StaleDataError("injected stale write")
    client.app.state.runtime.generate = broken
    item = await create(client, headers)
    run = await wait_run(client, headers, item["run_id"])
    assert run["status"] == "failed" and run["error_code"] == "WORKFLOW_FAILED"
    async with client.app.state.db.session_factory() as session:
        assert await session.scalar(select(Issue).where(Issue.issue_code == "WORKFLOW_FAILED"))


@pytest.mark.asyncio
async def test_thumbnail_is_compensated_when_document_commit_fails(client, monkeypatch):
    headers = await login(client)
    async def broken(*args, **kwargs):
        raise StaleDataError("injected document commit failure")
    monkeypatch.setattr(WorkflowExecutor, "commit_document", broken)
    raw = DocumentAdapter().export("# 小学数学\n\n平均分。", "分数", "docx")
    response = await client.post("/api/v1/personal-lessons", headers=headers,
        files={"file": ("lesson.docx", raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert response.status_code == 200
    run = await wait_run(client, headers, response.json()["data"]["run_id"])
    assert run["status"] == "failed"
    assert not list(client.app.state.settings.storage_root.glob("*.png"))


def test_control_cleanup_handles_nested_and_unclosed_input_without_cross_scanning():
    patch = SafePatchProcessor()
    assert patch.apply("前<analysis>内部<thinking>内部</thinking></analysis>后", {})[0] == "前后"
    assert patch.apply("前<analysis>内部", {})[0] == "前"
    for content in ["<a" * 60000, r"\(" * 60000, "课题：《" * 20000, r"\begin{" * 15000]:
        cleaned, _ = patch.apply(content, {})
        assert cleaned == content
    text = r"\(x<y\) " * 5000
    assert patch.apply(text, {})[0] == text.strip()


@pytest.mark.parametrize("database,redis", [
    ("postgresql+asyncpg://user@127.0.0.1:15432/lesson_test?host=untrusted.example&port=5432", "redis://127.0.0.1:16379/15"),
    ("postgresql+asyncpg://user@127.0.0.1:15432/lesson_test", "redis://127.0.0.1:16379/15?db=0"),
])
def test_evidence_dsn_cannot_redirect_with_query(monkeypatch, database, redis):
    monkeypatch.setenv("LESSON_EXTERNAL_DATABASE_URL", database)
    monkeypatch.setenv("LESSON_EXTERNAL_REDIS_URL", redis)
    with pytest.raises(ValueError, match="query"):
        endpoints()


def test_explicit_mock_local_settings_override_ambient_cloud_config(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "lesson_plan_ai")
    monkeypatch.setenv("STORAGE_BACKEND", "cos")
    settings = Settings(_env_file=None, environment="test", ai_provider="mock", storage_backend="local")
    assert settings.ai_provider == "mock" and settings.storage_backend == "local"


@pytest.mark.asyncio
async def test_upload_rejects_multiple_files_with_controlled_error(client):
    headers = await login(client)
    response = await client.post("/api/v1/personal-lessons", headers=headers,
        files=[("file", ("a.pdf", b"%PDF-1")), ("file", ("b.pdf", b"%PDF-1"))])
    assert response.status_code == 400 and response.json()["error"]["code"] == "HTTP_400"
