import io

import pytest
from PIL import Image


async def test_library_chunks_and_issue_sample_are_persisted_and_unique(tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from app.core.config import Settings
    from app.db.models import Base, Issue, PersonalLesson, User
    from app.db.session import Database

    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'library.db'}", _env_file=None))
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.session_factory() as session:
        user = User(email="library@example.test", password_hash="unused")
        session.add(user)
        await session.flush()
        lesson = PersonalLesson(user_id=user.id, tenant_id=user.tenant_id, filename="lesson.docx", file_path="source.docx",
                                chunks=[{"index": 0, "text": "数学课堂"}], thumbnail_path="thumb.png", thumbnail_kind="parsed_preview")
        sample = Issue(user_id=user.id, subsystem="generation", issue_type="run_sample", issue_code="run_completed",
                       record_kind="sample", sample_space="one_click", sample_key="run-sample:unique",
                       hour_bucket=datetime(2026, 10, 16, 10, tzinfo=UTC), sample_count=1, success_count=1,
                       duration_ms_sum=4000)
        session.add_all([lesson, sample])
        await session.commit()
        await session.refresh(lesson)
        await session.refresh(sample)
        assert lesson.chunks == [{"index": 0, "text": "数学课堂"}]
        assert lesson.thumbnail_kind == "parsed_preview"
        assert sample.failure_count == 0 and sample.observation_count == 1
        assert sample.hour_bucket.tzinfo == UTC
        session.add(Issue(subsystem="generation", issue_type="run_sample", issue_code="run_completed",
                          record_kind="sample", sample_key="run-sample:unique"))
        with pytest.raises(IntegrityError):
            await session.commit()
    await database.engine.dispose()


def test_library_sample_migration_upgrades_existing_0001_database(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    path = tmp_path / "old.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}", "ENVIRONMENT": "test"}

    def alembic(*arguments):
        result = subprocess.run([sys.executable, "-m", "alembic", *arguments], env=environment,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "0001")
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO system_issue_records(id, subsystem,issue_type,issue_code,severity,metadata_json,occurrence_count,created_at) VALUES('old','queue','failure','enqueue','warning','{}',1,'2026-10-16 00:00:00')")
        connection.commit()
    alembic("upgrade", "head")
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT record_kind,sample_count,observation_count FROM system_issue_records WHERE id='old'").fetchone()
        assert row == ("issue", 0, 1)
    alembic("check")
    alembic("downgrade", "0001")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT issue_code FROM system_issue_records WHERE id='old'").fetchone() == ("enqueue",)
    alembic("upgrade", "head")


def test_pdf_thumbnail_renders_original_first_page():
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    from app.integrations.thumbnails import create_thumbnail

    source = io.BytesIO()
    pdf = canvas.Canvas(source, pagesize=(600, 1200))
    pdf.setFillColor(colors.red)
    pdf.rect(0, 0, 600, 1200, fill=1, stroke=0)
    pdf.showPage()
    pdf.setFillColor(colors.blue)
    pdf.rect(0, 0, 600, 1200, fill=1, stroke=0)
    pdf.save()
    raw, kind, warnings = create_thumbnail("lesson.pdf", source.getvalue(), text="原文已解析")
    assert kind == "original_first_page" and warnings == []
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    assert raw.startswith(b"\x89PNG")
    assert image.width <= 480 and image.height <= 720
    red, green, blue = image.getpixel((image.width // 2, image.height // 2))
    assert red > 240 and green < 20 and blue < 20


def test_docx_thumbnail_marks_parsed_preview():
    from app.integrations.documents import DocumentAdapter
    from app.integrations.thumbnails import create_thumbnail

    adapter = DocumentAdapter()
    raw = adapter.export("# 数学课堂\n\n观察三角形的边和角，记录发现。", "数学课堂", "docx")
    parsed = adapter.parse_upload("lesson.docx", raw)
    image_bytes, kind, warnings = create_thumbnail("lesson.docx", raw, text=parsed["text"])
    assert kind == "parsed_preview"
    assert "thumbnail_parsed_preview_not_original_layout" in warnings
    image = Image.open(io.BytesIO(image_bytes))
    image.verify()
    assert image.width <= 480 and image.height <= 720
    visible = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    assert min(channel[0] for channel in visible.getextrema()) < 200


def test_thumbnail_rejects_broken_pdf_and_blank_parsed_preview():
    from app.integrations.documents import DocumentError
    from app.integrations.thumbnails import create_thumbnail

    for filename, raw, text in [("broken.pdf", b"%PDF-broken", "text"),
                                ("lesson.docx", b"PK\x03\x04bad", ""),
                                ("lesson.docx", b"PK\x03\x04bad", "nonempty untrusted text")]:
        with pytest.raises(DocumentError):
            create_thumbnail(filename, raw, text=text)
