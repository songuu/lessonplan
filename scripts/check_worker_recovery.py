"""Observe a real local knowledge-backed run across an operator-triggered Worker kill.

Creates only a synthetic teacher and lesson. Never kills processes or edits old data.
At CHECKPOINT_READY, the operator may kill the current local one-click Worker after
verifying its executable, command line and process start time. The supervisor restarts it.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import CallLog, Event, ModuleVersion, Run, RunCheckpoint  # noqa: E402
from scripts.migrate_local_sqlite import validate_target  # noqa: E402


def checkpoint_value_hash(payload):
    return hashlib.sha256(json.dumps(payload.get("value"), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def require_process_and_model_evidence(before_runtime, after_runtime, calls):
    if (after_runtime["instance"] != before_runtime["instance"] or
            after_runtime["processes"]["one-click"] == before_runtime["processes"]["one-click"]):
        raise RuntimeError("The same supervisor must replace the crashed one-click Worker process")
    if any(row["provider"] == "mock" for row in calls) or not any(
            row["provider"] == "openai_compatible" and row["status"] == "success" for row in calls):
        raise RuntimeError("Recovery requires a persisted successful real-model receipt, not configuration alone")


async def main():
    settings = get_settings()
    validate_target(settings.database_url)
    if settings.environment != "development" or settings.task_queue_mode != "arq" or settings.ai_provider == "mock":
        raise ValueError("Observer requires the real development PG/ARQ/model runtime")
    if settings.redis_url != "redis://127.0.0.1:56379/0":
        raise ValueError("Observer only targets this project's local Docker Redis")
    identifier = uuid4().hex
    output = ROOT / ".cache/worker-recovery" / identifier
    output.mkdir(parents=True)
    evidence = {"instance": identifier, "model": settings.llm_model, "completed": False}
    engine = create_async_engine(settings.database_url)
    watch_task = None
    sequences = []
    run_id = None
    try:
        async with asyncio.timeout(420), httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=360) as http:
            response = await http.post("/api/v1/auth/register", json={"email": f"recover-{identifier}@example.test",
                "password": uuid4().hex + "!Recover1", "name": "知识与恢复验收"})
            response.raise_for_status()
            headers = {"Authorization": "Bearer " + response.json()["data"]["access_token"]}

            async def data(method, path, payload=None, **kwargs):
                response = await http.request(method, "/api/v1" + path, headers=headers, json=payload, **kwargs)
                response.raise_for_status()
                return response.json()["data"]

            async def state():
                async with engine.connect() as conn:
                    run = (await conn.execute(select(Run.__table__).where(Run.id == run_id))).mappings().one()
                    checkpoints = (await conn.execute(select(RunCheckpoint.__table__).where(
                        RunCheckpoint.run_id == run_id, RunCheckpoint.checkpoint_key.like("module:%")))).mappings().all()
                    return {"status": run["status"], "epoch": run["execution_epoch"], "sequence": run["sequence"],
                        "recovery_count": run["recovery_count"], "job_id": run["job_id"], "error_code": run["error_code"],
                        "checkpoints": {row["checkpoint_key"]: {"id": row["id"], "value_hash": checkpoint_value_hash(row["payload"])} for row in checkpoints},
                        "versions": await conn.scalar(select(func.count()).select_from(ModuleVersion).where(ModuleVersion.draft_id == run["draft_id"]))}

            base = await data("POST", "/knowledge-bases", {"title": "几何检索恢复验收", "description": "公开数学常识合成资料"})
            # Distinctive but harmless source wording makes frozen retrieval inspectable.
            source = "# 长方形观察资料\n长方形有四个直角，对边相等。教学建议：先做方格纸观察，再折叠检验对边是否重合。正方形的四条边相等。"
            response = await http.post(f"/api/v1/knowledge-bases/{base['id']}/documents", headers=headers,
                files={"file": ("几何观察.md", source.encode(), "text/markdown")})
            response.raise_for_status()
            document = response.json()["data"]
            async with asyncio.timeout(30):
                while True:
                    indexed = await data("GET", "/knowledge-documents/" + document["id"])
                    if indexed["status"] == "failed":
                        raise RuntimeError("Knowledge indexing failed")
                    if indexed["status"] == "ready":
                        break
                    await asyncio.sleep(.2)
            hits = await data("POST", f"/knowledge-bases/{base['id']}/search", {"query": "长方形对边观察", "top_k": 3})
            assert hits["items"] and hits["items"][0]["document_id"] == document["id"]
            evidence["knowledge"] = {"base_id": base["id"], "document_id": document["id"],
                "chunks": indexed["chunk_count"], "retrieval_method": hits["method"], "score": hits["items"][0]["score"]}
            item = await data("POST", "/lesson-drafts/generations/one-click", {"request_id": identifier,
                "topic": "长方形与正方形的认识", "school_stage": "primary", "grade": "grade_3", "subject": "math",
                "lesson_type": "new_lesson", "total_duration_min": 40, "knowledge_base_ids": [base["id"]],
                "requirements": "每个模块约200字，教学过程3个环节共40分钟。结合提供的方格纸观察资料，突出对边、直角与辨析。"})
            run_id = item["run_id"]
            evidence.update(item)

            async def watch():
                async with http.stream("GET", f"/api/v1/generation-runs/{run_id}/events", headers=headers) as stream:
                    stream.raise_for_status()
                    kind = ""
                    async for line in stream.aiter_lines():
                        if line.startswith("event: "):
                            kind = line[7:]
                        elif line.startswith("data: ") and kind != "heartbeat":
                            event = json.loads(line[6:])
                            sequences.append(event["sequence"])

            watch_task = asyncio.create_task(watch())
            async with asyncio.timeout(180):
                while True:
                    before = await state()
                    if before["status"] in {"failed", "cancelled", "completed"}:
                        raise RuntimeError("Run ended before the requested crash point")
                    if len(before["checkpoints"]) >= 2:
                        break
                    await asyncio.sleep(.03)
            assert before["versions"] == 0 and before["epoch"] == 0
            runtime = json.loads((ROOT / ".cache/local-runtime/current.json").read_text(encoding="utf-8"))
            evidence["before_kill"] = before
            evidence["old_worker_pid"] = runtime["processes"]["one-click"]
            evidence["runtime_instance"] = runtime["instance"]
            (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print("CHECKPOINT_READY " + json.dumps({"run_id": run_id, "worker_pid": evidence["old_worker_pid"],
                "checkpoints": len(before["checkpoints"]), "evidence": str(output / "evidence.json")}), flush=True)
            started = time.monotonic()
            while True:
                final = await state()
                if final["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(.25)
            await watch_task
            assert final["status"] == "completed", f"Recovered run failed: {final['error_code']}"
            assert final["epoch"] >= 1 and final["recovery_count"] >= 1, "No actual crash recovery occurred"
            assert final["versions"] == 7
            for key, checkpoint in before["checkpoints"].items():
                assert final["checkpoints"][key] == checkpoint, "A validated checkpoint was regenerated or replaced"
            assert sequences == list(range(1, final["sequence"] + 1)), "SSE gap or duplicate across recovery"
            draft = await data("GET", "/lesson-drafts/" + item["draft_id"])
            assert draft["status"] == "ready"
            assert draft["knowledge_context_snapshot"][0]["document_id"] == document["id"]
            async with engine.connect() as conn:
                event_counts = dict((await conn.execute(select(Event.event_type, func.count()).where(Event.run_id == run_id)
                    .group_by(Event.event_type))).all())
                assert event_counts.get("run_completed") == 1
                assert not event_counts.get("run_failed") and not event_counts.get("run_cancelled")
                calls = [dict(row) for row in (await conn.execute(select(CallLog.model_name, CallLog.status,
                    CallLog.provider).where(CallLog.run_id == run_id))).mappings()]
            after_runtime = json.loads((ROOT / ".cache/local-runtime/current.json").read_text(encoding="utf-8"))
            require_process_and_model_evidence(runtime, after_runtime, calls)
            kill = json.loads((output / "kill-receipt.json").read_text(encoding="utf-8"))
            assert kill["pid"] == evidence["old_worker_pid"] and kill["runtime_instance"] == runtime["instance"]
            assert kill["identity_verified"] and kill["action"] == "hard_stop"
            evidence.update(completed=True, after_recovery=final, event_counts=event_counts,
                model_call_receipts=calls, kill_receipt=kill,
                recovered_ms=round((time.monotonic() - started) * 1000), sse_events=len(sequences),
                new_worker_pid=after_runtime["processes"]["one-click"])
            print("RECOVERY_PASSED " + str(output / "evidence.json"), flush=True)
    finally:
        if watch_task and not watch_task.done():
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", required=True, action="store_true")
    parser.parse_args()
    asyncio.run(main())
