import pytest
from jsonschema import ValidationError
from sqlalchemy import select

from app.api.lessons import style_context
from app.db.models import PersonalLesson, User
from app.services.contracts import stream_event_validator
from tests.conftest import login


def test_persisted_stream_contract_rejects_unknown_events_and_non_text_deltas():
    validator = stream_event_validator()
    validator.validate({"run_id": "run", "sequence": 1, "event_type": "content_delta", "payload": {"delta": "正文"}})
    for kind, payload in [("invented_terminal", {}), ("content_delta", {"delta": 42}), ("module_done", {})]:
        with pytest.raises(ValidationError):
            validator.validate({"run_id": "run", "sequence": 2, "event_type": kind, "payload": payload})


@pytest.mark.asyncio
async def test_style_keeps_nearby_personal_match_but_rejects_below_threshold(client):
    await login(client)
    async with client.app.state.db.session_factory() as session:
        user = await session.scalar(select(User))
        session.add(PersonalLesson(user_id=user.id, tenant_id=user.tenant_id, filename="math.docx", file_path="opaque",
            status="ready", school_stage="senior", grade="grade_12", subject="math", lesson_type="new_lesson",
            style_summary={"table_count": 2, "average_paragraph_chars": 35}))
        await session.commit()
        nearby = await style_context(session, user, {"school_stage": "primary", "grade": "grade_3", "subject": "math", "lesson_type": "new_lesson"})
        assert nearby["source"] == "personal" and nearby["effective"] and nearby["reference_count"] == 1
        unrelated = await style_context(session, user, {"school_stage": "primary", "grade": "grade_3", "subject": "english", "lesson_type": "new_lesson"})
        assert unrelated["source"] == "system" and unrelated["reference_count"] == 0
        assert unrelated["system_style_id"] == "primary_tasks"
