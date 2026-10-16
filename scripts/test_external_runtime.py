"""Opt-in evidence runner for a dedicated loopback PostgreSQL/Redis instance.

Never drops databases, truncates tables, flushes Redis, or starts system services.
The database name must be lesson_test; use an empty dedicated Redis DB.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from arq import cron  # noqa: E402
from arq.connections import RedisSettings, create_pool  # noqa: E402
from arq.jobs import Job  # noqa: E402
from arq.worker import Worker  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm.exc import StaleDataError  # noqa: E402
from starlette.requests import Request as StarletteRequest  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.models import Draft, Outbox, PersonalLesson, Run, User  # noqa: E402
from app.factory import create_app  # noqa: E402
from app.services.lessons import WorkflowExecutor, create_run  # noqa: E402
from app.tasks.queue import QUEUES  # noqa: E402
from app.tasks.workers import execute_run, maintenance  # noqa: E402


def endpoints() -> tuple[str, str]:
    database = os.environ.get("LESSON_EXTERNAL_DATABASE_URL", "")
    redis = os.environ.get("LESSON_EXTERNAL_REDIS_URL", "")
    db_url = make_url(database)
    redis_url = urlsplit(redis)
    if db_url.query or urlsplit(database).fragment or redis_url.query or redis_url.fragment:
        raise ValueError("Evidence DSNs must not contain query parameters or fragments")
    if db_url.drivername != "postgresql+asyncpg" or db_url.database != "lesson_test" or db_url.host != "127.0.0.1":
        raise ValueError("Use a dedicated postgresql+asyncpg://...@127.0.0.1:<port>/lesson_test URL")
    if db_url.port in {None, 5432} or redis_url.hostname != "127.0.0.1" or redis_url.port in {None, 6379}:
        raise ValueError("Use explicit non-default loopback ports for isolated evidence services")
    if redis_url.scheme != "redis" or not redis_url.path.strip("/").isdigit():
        raise ValueError("Use an explicit dedicated Redis database index")
    return database, redis


async def prepare_database(database: str) -> dict:
    url = make_url(database)
    connect_args = dict(host=url.host, port=url.port, user=url.username, password=url.password)
    admin = await asyncpg.connect(database="postgres", **connect_args)
    try:
        exists = await admin.fetchval("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = $1)", "lesson_test")
        if not exists:
            await admin.execute("CREATE DATABASE lesson_test")
    finally:
        await admin.close()
    connection = await asyncpg.connect(database="lesson_test", **connect_args)
    try:
        before = await connection.fetchval("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
        version = await connection.fetchval("SELECT version()")
    finally:
        await connection.close()
    environment = dict(os.environ, DATABASE_URL=database, ENVIRONMENT="test")
    migrated = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "head"],
                                      cwd=ROOT, env=environment, capture_output=True, text=True, check=True)
    if migrated.returncode != 0:
        raise AssertionError("Alembic migration failed")
    await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", "check"],
                            cwd=ROOT, env=environment, capture_output=True, text=True, check=True)
    connection = await asyncpg.connect(database="lesson_test", **connect_args)
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        after = await connection.fetchval("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
    finally:
        await connection.close()
    expected_head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    assert revision == expected_head
    return {"server_version": version, "public_tables_before_migration": before,
            "public_tables_after_migration": after, "alembic_head": revision, "matches_local_head": True,
            "alembic_check": "No new upgrade operations detected"}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def parse_sse(source: str) -> list[dict]:
    output = []
    for block in source.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "id" in fields:
            output.append({"sequence": int(fields["id"]), "event": fields["event"],
                           "data": json.loads(fields["data"])})
    return output


async def run_evidence() -> dict:
    database, redis_url = endpoints()
    identifier = uuid4().hex
    started = time.monotonic()
    evidence = {"instance": identifier, "levels": ["E2", "E3"], "ai_provider": "explicit_mock",
                "transport": "real_loopback_HTTP", "worker_process_mode": "seven_ARQ_workers_in_one_python_process",
                "scenarios": {}, "limitations": ["No production deployment or real LLM/COS calls",
                    "No operating-system worker kill/restart or multiprocess load testing"]}
    evidence["postgresql"] = await prepare_database(database)
    probe = await create_pool(RedisSettings.from_dsn(redis_url))
    if await probe.dbsize():
        await probe.aclose()
        raise ValueError("Dedicated Redis DB is not empty; refusing to flush or reuse unknown keys")
    evidence["redis"] = {"server_version": (await probe.info("server"))["redis_version"],
                         "database_index": urlsplit(redis_url).path, "initial_key_count": 0}
    await probe.aclose()
    settings = Settings(_env_file=None, environment="test", database_url=database, redis_url=redis_url,
                        task_queue_mode="arq", ai_provider="mock", run_timeout_seconds=30,
                        storage_backend="local",
                        storage_root=ROOT / ".cache/infrastructure/artifacts" / identifier,
                        frontend_dist=ROOT / ".cache/infrastructure/no-frontend")
    assert settings.ai_provider == "mock" and settings.storage_backend == "local"
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    server_task = asyncio.create_task(server.serve())
    workers: list[Worker] = []
    tasks: list[asyncio.Task] = []
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise AssertionError("HTTP server exited before startup")
                await asyncio.sleep(.05)
        for queue, concurrency in QUEUES.items():
            worker = Worker([execute_run], redis_settings=RedisSettings.from_dsn(redis_url),
                queue_name=f"lesson:{queue}", health_check_key=f"lesson:{queue}:health",
                max_jobs=concurrency, job_timeout=60, max_tries=1, retry_jobs=False, poll_delay=.03,
                health_check_interval=.5, handle_signals=True, allow_abort_jobs=True, log_results=False,
                ctx={"app": app}, cron_jobs=[cron(maintenance, second={0, 15, 30, 45}, run_at_startup=True)]
                if queue == "maintenance" else [])
            workers.append(worker)
            tasks.append(asyncio.create_task(worker.async_run()))
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            for _ in range(100):
                health = await client.get("/health")
                if health.status_code == 200:
                    break
                await asyncio.sleep(.05)
            assert health.status_code == 200, health.text
            evidence["scenarios"]["seven_worker_heartbeats"] = health.json()["workers"]
            registered = await client.post("/api/v1/auth/register", json={"email": f"external-{identifier}@example.test",
                "password": "Local!Evidence12345", "name": "本地证据教师"})
            assert registered.status_code in {200, 201}, registered.text
            headers = {"Authorization": "Bearer " + registered.json()["data"]["access_token"]}
            original_form = StarletteRequest.form
            original_upload_limit = settings.upload_max_bytes
            form_calls = []

            def track_form(request, *args, **kwargs):
                form_calls.append(True)
                return original_form(request, *args, **kwargs)

            StarletteRequest.form = track_form
            settings.upload_max_bytes = 1024
            try:
                media = {"Content-Type": "multipart/form-data; boundary=evidence"}
                unauthorized = await client.post("/api/v1/personal-lessons", headers=media, content=b"invalid multipart")
                assert unauthorized.status_code == 401 and not form_calls

                async def oversized_chunked():
                    for _ in range(8):
                        yield b"x" * 2048

                oversized = await client.post("/api/v1/personal-lessons", headers={**headers, **media}, content=oversized_chunked())
                assert oversized.status_code == 413 and not form_calls
                evidence["scenarios"]["upload_request_boundary"] = {"anonymous_status": 401,
                    "authenticated_chunked_oversize_status": 413, "multipart_parser_calls": 0}
            finally:
                StarletteRequest.form = original_form
                settings.upload_max_bytes = original_upload_limit
            payload = {"request_id": identifier, "topic": "分数的初步认识", "school_stage": "primary",
                       "grade": "三年级", "subject": "数学", "lesson_type": "new_lesson", "total_duration_min": 40}
            run_ids = []

            async def create(body=payload, key="create", mode="one-click"):
                response = await client.post(f"/api/v1/lesson-drafts/generations/{mode}",
                    headers={**headers, "Idempotency-Key": f"{identifier}-{key}"}, json=body)
                assert response.status_code in {200, 201, 202}, response.text
                value = response.json()["data"]
                if value["run_id"] not in run_ids:
                    run_ids.append(value["run_id"])
                return value

            async def wait(run_id, expected="completed"):
                for _ in range(500):
                    response = await client.get(f"/api/v1/generation-runs/{run_id}", headers=headers)
                    assert response.status_code == 200, response.text
                    value = response.json()["data"]
                    if value["status"] in {"completed", "failed", "cancelled"}:
                        assert value["status"] == expected, value
                        return value
                    await asyncio.sleep(.03)
                raise AssertionError("ARQ Run failed to reach terminal status within 15 seconds")

            async def wait_arq_job(run_id):
                async with app.state.db.session_factory() as database_session:
                    record = await database_session.get(Run, run_id)
                    job_id, queue_name = record.job_id, record.queue_name
                return await Job(job_id, app.state.dispatcher.redis, _queue_name=queue_name).result(timeout=10, poll_delay=.03)

            first, duplicate = await asyncio.gather(create(), create())
            assert first == duplicate
            streamed = await client.get(f"/api/v1/generation-runs/{first['run_id']}/events", headers=headers)
            events = parse_sse(streamed.text)
            await wait(first["run_id"])
            terminals = [e for e in events if e["event"] in {"run_completed", "run_failed", "run_cancelled"}]
            assert len(terminals) == 1 and terminals[0]["event"] == "run_completed"
            assert [e["sequence"] for e in events] == list(range(1, events[-1]["sequence"] + 1))
            cursor = events[len(events) // 2]["sequence"]
            replay = parse_sse((await client.get(f"/api/v1/generation-runs/{first['run_id']}/events",
                headers={**headers, "Last-Event-ID": str(cursor)})).text)
            assert replay == [e for e in events if e["sequence"] > cursor]
            conflict = await client.post("/api/v1/lesson-drafts/generations/one-click",
                headers={**headers, "Idempotency-Key": f"{identifier}-create"}, json={**payload, "topic": "三角形"})
            assert conflict.status_code == 409
            evidence["scenarios"]["idempotency_sse_replay"] = {"same_request_reused": True, "conflict_status": 409,
                "event_count": len(events), "replayed_count": len(replay), "unique_terminal": True}

            redis = app.state.dispatcher.redis
            duplicate_job = await redis.enqueue_job("execute_run", first["run_id"], _queue_name="lesson:one-click",
                                                    _job_id=f"duplicate-{identifier}")
            assert duplicate_job is not None
            await duplicate_job.result(timeout=10, poll_delay=.03)
            unchanged = parse_sse((await client.get(f"/api/v1/generation-runs/{first['run_id']}/events", headers=headers)).text)
            assert unchanged == events
            evidence["scenarios"]["duplicate_delivery"] = {"unchanged_event_count": len(events), "run_cas_preserved": True}

            path = f"/api/v1/lesson-drafts/{first['draft_id']}"
            draft = (await client.get(path, headers=headers)).json()["data"]
            assert draft["status"] == "ready" and len(draft["modules"]) == 7
            export = await client.post(f"{path}/exports", headers=headers, json={"format": "docx"})
            assert export.status_code == 200, export.text
            exported = export.json()["data"]
            run_ids.append(exported["run_id"])
            await wait(exported["run_id"])
            download = await client.get(f"/api/v1/lesson-exports/{exported['id']}/download", headers=headers)
            assert download.status_code == 200 and download.content.startswith(b"PK")
            evidence["scenarios"]["export_queue_readback"] = {"bytes": len(download.content),
                "sha256": hashlib.sha256(download.content).hexdigest()}

            upload = await client.post("/api/v1/personal-lessons", headers=headers,
                files={"file": ("evidence.docx", download.content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
            assert upload.status_code in {200, 201}, upload.text
            uploaded = upload.json()["data"]
            run_ids.append(uploaded["run_id"])
            await wait(uploaded["run_id"])
            listed = (await client.get("/api/v1/personal-lessons", headers=headers)).json()["data"]
            listed_document = next(item for item in listed if item["id"] == uploaded["id"])
            assert listed_document["thumbnail_url"]
            thumbnail = await client.get(listed_document["thumbnail_url"], headers=headers)
            assert thumbnail.status_code == 200 and thumbnail.content.startswith(b"\x89PNG\r\n\x1a\n")
            async with app.state.db.session_factory() as database_session:
                document_record = await database_session.get(PersonalLesson, uploaded["id"])
                assert document_record.chunks and document_record.thumbnail_path
                chunk_count = len(document_record.chunks)
            evidence["scenarios"]["document_queue"] = {"run_id": uploaded["run_id"], "status": "completed",
                "persisted_chunks": chunk_count, "thumbnail_bytes": len(thumbnail.content),
                "thumbnail_sha256": hashlib.sha256(thumbnail.content).hexdigest()}

            before_delete_files = {file.name for file in settings.storage_root.iterdir() if file.is_file()}
            original_commit_document = WorkflowExecutor.commit_document
            commit_entered, commit_release = asyncio.Event(), asyncio.Event()
            pending_thumbnail_keys = []

            async def delayed_commit(executor, run_id, document_payload, parsed, thumbnail_key, thumbnail_kind, warnings):
                assert thumbnail_key and await asyncio.to_thread(app.state.storage.get, thumbnail_key)
                pending_thumbnail_keys.append(thumbnail_key)
                commit_entered.set()
                await commit_release.wait()
                return await original_commit_document(executor, run_id, document_payload, parsed,
                                                       thumbnail_key, thumbnail_kind, warnings)

            WorkflowExecutor.commit_document = delayed_commit
            try:
                deletion_upload = await client.post("/api/v1/personal-lessons", headers=headers,
                    files={"file": ("delete-during-parse.docx", download.content,
                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
                assert deletion_upload.status_code == 200, deletion_upload.text
                deletion_record = deletion_upload.json()["data"]
                run_ids.append(deletion_record["run_id"])
                await asyncio.wait_for(commit_entered.wait(), 10)
                deleted = await client.delete(f"/api/v1/personal-lessons/{deletion_record['id']}", headers=headers)
                assert deleted.status_code == 200, deleted.text
                await wait(deletion_record["run_id"], "cancelled")
                commit_release.set()
                await wait_arq_job(deletion_record["run_id"])
            finally:
                commit_release.set()
                WorkflowExecutor.commit_document = original_commit_document
            deletion_events = parse_sse((await client.get(
                f"/api/v1/generation-runs/{deletion_record['run_id']}/events", headers=headers)).text)
            assert [e["event"] for e in deletion_events if e["event"] in {"run_completed", "run_failed", "run_cancelled"}] == ["run_cancelled"]
            async with app.state.db.session_factory() as database_session:
                assert await database_session.get(PersonalLesson, deletion_record["id"]) is None
            assert {file.name for file in settings.storage_root.iterdir() if file.is_file()} == before_delete_files
            evidence["scenarios"]["delete_personal_lesson_during_parse"] = {"deleted_before_commit": True,
                "unique_terminal": "run_cancelled", "temporary_png_compensated": True, "orphan_files": 0}

            before_failed_pngs = {file.name for file in settings.storage_root.glob("*.png")}

            async def failing_commit(*args, **kwargs):
                raise StaleDataError("Injected ORM commit failure with code=None")

            WorkflowExecutor.commit_document = failing_commit
            try:
                failing_upload = await client.post("/api/v1/personal-lessons", headers=headers,
                    files={"file": ("failed-commit.docx", download.content,
                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
                assert failing_upload.status_code == 200, failing_upload.text
                failing_record = failing_upload.json()["data"]
                run_ids.append(failing_record["run_id"])
                failed_run = await wait(failing_record["run_id"], "failed")
                assert failed_run["error_code"] == "WORKFLOW_FAILED"
                await wait_arq_job(failing_record["run_id"])
            finally:
                WorkflowExecutor.commit_document = original_commit_document
            assert {file.name for file in settings.storage_root.glob("*.png")} == before_failed_pngs
            await client.delete(f"/api/v1/personal-lessons/{failing_record['id']}", headers=headers)
            evidence["scenarios"]["failed_document_commit_compensation"] = {"none_code_fallback": "WORKFLOW_FAILED",
                "temporary_png_compensated": True, "run_status": "failed"}

            module = draft["modules"][0]
            session_response = await client.post("/api/v1/module-polish-sessions", headers=headers,
                json={"draft_id": first["draft_id"], "module_id": module["module_id"], "scope": "module"})
            assert session_response.status_code == 200, session_response.text
            polish_session = session_response.json()["data"]
            polished = await client.post(f"/api/v1/module-polish-sessions/{polish_session['id']}/messages", headers=headers,
                json={"instruction": "表达更简洁", "working_content": module["teacher_view_content"]})
            assert polished.status_code == 200, polished.text
            polish_run = polished.json()["data"]["run_id"]
            run_ids.append(polish_run)
            await wait(polish_run)
            after = (await client.get(path, headers=headers)).json()["data"]
            assert after["modules"][0]["current_version_id"] == module["current_version_id"]
            evidence["scenarios"]["polish_queue_candidate_isolation"] = True

            structured_payload = {**payload, "request_id": identifier + "-structured", "modules": [
                {"module_type": "teaching_objectives", "display_name": "教学目标", "enabled": True},
                {"module_type": "teaching_process", "display_name": "教学过程", "enabled": True}]}
            structured = await create(structured_payload, key="structured", mode="interactive")
            await wait(structured["run_id"])
            structured_path = f"/api/v1/lesson-drafts/{structured['draft_id']}"

            async def current_structured():
                return (await client.get(structured_path, headers=headers)).json()["data"]

            async def confirm(draft, module, **extra):
                response = await client.post(f"{structured_path}/interactive/confirm", headers=headers,
                    json={"module_id": module["module_id"], "expected_content_version": draft["content_version"],
                          "expected_current_module_version_id": module["current_version_id"],
                          "expected_module_config_version_id": draft["module_config_version_id"], **extra})
                assert response.status_code == 200, response.text

            current = await current_structured()
            await confirm(current, current["modules"][0])
            process_id = current["modules"][1]["module_id"]
            outline_response = await client.post(f"{structured_path}/interactive/step", headers=headers,
                json={"module_id": process_id, "stage": "outline"})
            assert outline_response.status_code == 200, outline_response.text
            outline_run = outline_response.json()["data"]["run_id"]
            run_ids.append(outline_run)
            await wait(outline_run)
            current = await current_structured()
            outline = [{"stage_id": "teacher-first", "title": "折纸观察", "duration_min": 13, "objective": "观察整体与部分"},
                       {"stage_id": "teacher-second", "title": "解释迁移", "duration_min": 31, "objective": "说明理由并迁移"}]
            await confirm(current, current["modules"][1], outline=outline)
            premature = await client.post(f"{structured_path}/interactive/finalize", headers=headers, json={})
            assert premature.status_code == 409
            activities_response = await client.post(f"{structured_path}/interactive/step", headers=headers,
                json={"module_id": process_id, "stage": "activities"})
            assert activities_response.status_code == 200, activities_response.text
            activities_run = activities_response.json()["data"]["run_id"]
            run_ids.append(activities_run)
            await wait(activities_run)
            current = await current_structured()
            process_content = current["modules"][1]["teacher_view_content"]
            assert process_content.startswith("### 折纸观察（13分钟）")
            assert "### 解释迁移（31分钟）" in process_content
            await confirm(current, current["modules"][1])
            finalized = await client.post(f"{structured_path}/interactive/finalize", headers=headers, json={})
            assert finalized.status_code == 200, finalized.text
            assert (await current_structured())["status"] == "ready"
            activities_events = parse_sse((await client.get(f"/api/v1/generation-runs/{activities_run}/events", headers=headers)).text)
            assert [e["data"]["stage_id"] for e in activities_events if e["event"] == "stage_started"] == ["teacher-first", "teacher-second"]
            assert any(e["event"] == "module_progress" and e["data"].get("phase") == "blueprint_reused" for e in activities_events)
            evidence["scenarios"]["structured_queue"] = {"three_stages_completed": True,
                "teacher_outline_duration_sum": 44, "original_duration": 40, "outline_preserved": True,
                "blueprint_reused": True, "finalize_before_activities_status": 409}

            original_generate = app.state.runtime.generate
            entered, release = asyncio.Event(), asyncio.Event()

            async def delayed_generate(inputs, modules, **kwargs):
                if inputs["topic"] == "取消运行证据":
                    entered.set()
                    await release.wait()
                return await original_generate(inputs, modules, **kwargs)

            app.state.runtime.generate = delayed_generate
            cancelled = await create({**payload, "topic": "取消运行证据", "request_id": identifier + "-cancel"}, key="cancel")
            await asyncio.wait_for(entered.wait(), 10)
            cancel_response = await client.post(f"/api/v1/generation-runs/{cancelled['run_id']}/cancel", headers=headers)
            assert cancel_response.status_code == 200, cancel_response.text
            release.set()
            await wait(cancelled["run_id"], "cancelled")
            await asyncio.sleep(.2)
            cancelled_events = parse_sse((await client.get(f"/api/v1/generation-runs/{cancelled['run_id']}/events", headers=headers)).text)
            assert [e["event"] for e in cancelled_events if e["event"].startswith("run_") and e["event"] != "run_started"] == ["run_cancelled"]
            cancelled_draft = (await client.get(f"/api/v1/lesson-drafts/{cancelled['draft_id']}", headers=headers)).json()["data"]
            assert all(not m["current_version_id"] for m in cancelled_draft["modules"])
            app.state.runtime.generate = original_generate
            evidence["scenarios"]["running_cancel_late_commit_guard"] = True

            # Pause only these owned Workers; remove only the exact stable job just created.
            workers[0].allow_pick_jobs = workers[-1].allow_pick_jobs = False
            missing = await create({**payload, "request_id": identifier + "-redis-loss"}, key="redis-loss")
            async with app.state.db.session_factory() as database_session:
                queued_run = await database_session.get(Run, missing["run_id"])
                assert queued_run.status == "queued"
                job_id, queue_name = queued_run.job_id, queued_run.queue_name
                intent = await database_session.scalar(select(Outbox).where(Outbox.run_id == queued_run.id,
                    Outbox.event_type == "dispatch_run"))
                assert intent.status == "published"
            assert await redis.delete(f"arq:job:{job_id}") == 1
            assert await redis.zrem(queue_name, job_id) == 1
            completed_before_recovery = workers[-1].jobs_complete
            await redis.enqueue_job(workers[-1].cron_jobs[0].name, _queue_name="lesson:maintenance",
                                    _job_id=f"recover-lost-{identifier}")
            workers[-1].allow_pick_jobs = True
            for _ in range(200):
                if workers[-1].jobs_complete > completed_before_recovery:
                    break
                await asyncio.sleep(.03)
            assert await redis.exists(f"arq:job:{job_id}")
            assert await redis.zscore(queue_name, job_id) is not None
            workers[0].allow_pick_jobs = True
            await wait(missing["run_id"])
            async with app.state.db.session_factory() as database_session:
                intent = await database_session.scalar(select(Outbox).where(Outbox.run_id == missing["run_id"],
                    Outbox.event_type == "dispatch_run"))
                assert intent.status == "published" and intent.attempts >= 2
            evidence["scenarios"]["published_queued_job_loss_recovery"] = {"stable_job_id_reused": True,
                "outbox_attempts": intent.attempts, "recovered_by_real_maintenance_worker": True}

            async def live_stream_case(key, *, disable_pubsub=False):
                stream_entered, stream_release = asyncio.Event(), asyncio.Event()

                async def controlled_generate(inputs, modules, **kwargs):
                    if inputs["topic"] == key:
                        stream_entered.set()
                        await stream_release.wait()
                    return await original_generate(inputs, modules, **kwargs)

                class NoPubSub:
                    def __getattr__(self, name):
                        if name == "pubsub":
                            raise ConnectionError("Injected PubSub subscription failure")
                        return getattr(redis, name)

                app.state.runtime.generate = controlled_generate
                stream_run = await create({**payload, "request_id": identifier + key, "topic": key},
                                          key="stream-fallback" if disable_pubsub else "stream-pubsub")
                await asyncio.wait_for(stream_entered.wait(), 10)
                channel = f"lesson:run:{stream_run['run_id']}"
                observer = redis.pubsub()
                await observer.subscribe(channel)
                await observer.get_message(timeout=1)
                if disable_pubsub:
                    app.state.dispatcher.redis = NoPubSub()

                async def consume_live():
                    lines = []
                    async with client.stream("GET", f"/api/v1/generation-runs/{stream_run['run_id']}/events", headers=headers) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            lines.append(line)
                    return parse_sse("\n".join(lines) + "\n")

                consuming = asyncio.create_task(consume_live())
                try:
                    expected_subscribers = 1 if disable_pubsub else 2
                    for _ in range(100):
                        subscribers = (await redis.pubsub_numsub(channel))[0][1]
                        if subscribers >= expected_subscribers:
                            break
                        await asyncio.sleep(.03)
                    assert subscribers >= expected_subscribers
                    if disable_pubsub:
                        await asyncio.sleep(.1)
                    stream_release.set()
                    live_events = await asyncio.wait_for(consuming, 10)
                    await wait(stream_run["run_id"])
                    assert len([e for e in live_events if e["event"] == "run_completed"]) == 1
                    message = None
                    for _ in range(20):
                        message = await observer.get_message(ignore_subscribe_messages=True, timeout=.2)
                        if message:
                            break
                    assert message and message["data"] == b"changed"
                    assert [e["sequence"] for e in live_events] == list(range(1, live_events[-1]["sequence"] + 1))
                finally:
                    stream_release.set()
                    app.state.dispatcher.redis = redis
                    app.state.runtime.generate = original_generate
                    await observer.aclose()
                for _ in range(30):
                    remaining_subscribers = (await redis.pubsub_numsub(channel))[0][1]
                    if remaining_subscribers == 0:
                        break
                    await asyncio.sleep(.03)
                assert remaining_subscribers == 0
                return {"connected_before_terminal": True, "event_count": len(live_events),
                        "pubsub_wakeup_payload": "changed", "subscriber_connections_released": True,
                        "injected_subscription_failure": disable_pubsub}

            evidence["scenarios"]["live_sse_pubsub"] = await live_stream_case("PubSub流式证据")
            evidence["scenarios"]["sse_database_poll_fallback"] = await live_stream_case("SSE回退证据", disable_pubsub=True)

            async with app.state.db.session_factory() as database_session:
                owned_draft = await database_session.get(Draft, first["draft_id"])
                user = await database_session.get(User, owned_draft.user_id)
                review = await create_run(database_session, owned_draft, user, "lesson_internal_review", {}, f"review-{identifier}")
                review_id = review.id
                await database_session.commit()
            run_ids.append(review_id)
            await app.state.dispatcher.submit(review_id)
            await wait(review_id)
            evidence["scenarios"]["internal_review_queue"] = True
            maintenance_completed = workers[-1].jobs_complete
            maintenance_job = await redis.enqueue_job(workers[-1].cron_jobs[0].name,
                _queue_name="lesson:maintenance", _job_id=f"maintenance-evidence-{identifier}")
            assert maintenance_job is not None
            # Cron functions intentionally retain no result, so observe the actual Worker receipt.
            for _ in range(200):
                if workers[-1].jobs_complete > maintenance_completed:
                    break
                await asyncio.sleep(.03)
            assert workers[-1].jobs_complete > maintenance_completed
            assert await redis.xlen("lesson:domain-events") > 0
            evidence["scenarios"]["maintenance_outbox_stream"] = {"stream_length": await redis.xlen("lesson:domain-events")}

            async with app.state.db.session_factory() as database_session:
                persisted = (await database_session.scalars(select(Run).where(Run.id.in_(run_ids)))).all()
                evidence["run_ids"] = run_ids
                evidence["run_statuses"] = {r.id: r.status for r in persisted}
            url = make_url(database)
            independent = await asyncpg.connect(host=url.host, port=url.port, database="lesson_test",
                                               user=url.username, password=url.password)
            try:
                evidence["independent_readback"] = dict(await independent.fetchrow("""
                    SELECT (SELECT COUNT(*) FROM ai_generation_runs WHERE id=ANY($1::varchar[])) AS runs,
                           (SELECT COUNT(*) FROM ai_generation_events WHERE run_id=ANY($1::varchar[])) AS events,
                           (SELECT COUNT(*) FROM ai_call_logs WHERE run_id=ANY($1::varchar[])) AS call_logs,
                           (SELECT COUNT(*) FROM quality_check_results WHERE run_id=ANY($1::varchar[])) AS quality_results,
                           (SELECT COUNT(*) FROM domain_outbox_events WHERE run_id=ANY($1::varchar[])) AS outbox
                    """, run_ids))
                sample_readback = dict(await independent.fetchrow("""
                    SELECT SUM(sample_count)::int AS samples, SUM(success_count)::int AS successes,
                           SUM(failure_count)::int AS failures, SUM(cancelled_count)::int AS cancelled
                    FROM system_issue_records WHERE record_kind='sample' AND user_id=$1
                    """, draft["user_id"]))
                actual_terminal = dict(await independent.fetchrow("""
                    SELECT COUNT(*)::int AS samples, COUNT(*) FILTER(WHERE status='completed')::int AS successes,
                           COUNT(*) FILTER(WHERE status='failed')::int AS failures,
                           COUNT(*) FILTER(WHERE status='cancelled')::int AS cancelled
                    FROM ai_generation_runs WHERE user_id=$1 AND status IN ('completed','failed','cancelled')
                    """, draft["user_id"]))
                assert sample_readback == actual_terminal, (sample_readback, actual_terminal)
                evidence["scenarios"]["terminal_sample_denominators"] = sample_readback
            finally:
                await independent.close()
            evidence["workers"] = {w.queue_name: {"completed": w.jobs_complete, "failed": w.jobs_failed} for w in workers}
    finally:
        for worker in workers:
            worker.handle_sig(signal.SIGTERM)
        await asyncio.gather(*tasks, return_exceptions=True)
        for worker in workers:
            await worker.close()
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return evidence


def main():
    evidence = asyncio.run(run_evidence())
    destination = ROOT / ".cache/infrastructure/evidence.json"
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
