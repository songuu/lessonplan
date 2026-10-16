import io

import pytest
from sqlalchemy import select

from app.db.models import Audit, Issue, PersonalLesson, User
from app.integrations.documents import DocumentAdapter
from tests.conftest import login
from tests.test_workflows import create, wait_run


@pytest.mark.asyncio
async def test_admin_enforces_role_and_records_audit(client):
    headers = await login(client)
    assert (await client.get("/api/v1/admin/users", headers=headers)).status_code == 403
    async with client.app.state.db.session_factory() as session:
        user = await session.scalar(select(User))
        user.role = "admin"
        user_id = user.id
        await session.commit()
    users = await client.get("/api/v1/admin/users", headers=headers)
    assert users.status_code == 200
    assert "password_hash" not in users.json()["data"][0]
    assert (await client.patch(f"/api/v1/admin/users/{user_id}", headers=headers, json={"is_active": False})).status_code == 409
    async with client.app.state.db.session_factory() as session:
        assert (await session.scalars(select(Audit))).all()


@pytest.mark.asyncio
async def test_personal_upload_chunks_thumbnail_and_frozen_style(client):
    headers = await login(client)
    raw = DocumentAdapter().export("# 小学三年级 数学 复习课\n\n分数复习与错例辨析。", "分数", "docx")
    uploaded = await client.post("/api/v1/personal-lessons", headers=headers,
        files={"file": ("分数.docx", raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert uploaded.status_code == 200, uploaded.text
    item = uploaded.json()["data"]
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
    listed = (await client.get("/api/v1/personal-lessons", headers=headers)).json()["data"][0]
    assert listed["status"] == "ready" and listed["grade"] == "grade_3"
    assert listed["thumbnail_kind"] == "parsed_preview"
    thumbnail = await client.get(listed["thumbnail_url"], headers=headers)
    from PIL import Image
    assert Image.open(io.BytesIO(thumbnail.content)).format == "PNG"
    async with client.app.state.db.session_factory() as session:
        document = await session.get(PersonalLesson, item["id"])
        assert document.chunks and document.chunks[0]["text"]
    draft = await create(client, headers)
    await wait_run(client, headers, draft["run_id"])
    detail = (await client.get(f"/api/v1/lesson-drafts/{draft['draft_id']}", headers=headers)).json()["data"]
    style = detail["style_intent_snapshot"]
    assert style["source"] == "personal" and style["reference_count"] == 1
    assert (await client.delete(f"/api/v1/personal-lessons/{item['id']}", headers=headers)).status_code == 200
    after = (await client.get(f"/api/v1/lesson-drafts/{draft['draft_id']}", headers=headers)).json()["data"]
    assert after["style_intent_snapshot"] == style
    async with client.app.state.db.session_factory() as session:
        samples = (await session.scalars(select(Issue).where(Issue.record_kind == "sample"))).all()
        assert sum(row.sample_count for row in samples) == 2
        assert sum(row.success_count for row in samples) == 2
