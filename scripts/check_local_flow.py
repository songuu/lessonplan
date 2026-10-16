"""Opt-in REAL MODEL canary against the running local API, PostgreSQL and Redis.

Creates a synthetic teacher and two seven-module lessons; consumes model tokens.
Does not modify existing teachers' lessons or delete any records.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.models import CallLog, Event, ModuleVersion, Run  # noqa: E402
from scripts.migrate_local_sqlite import validate_target  # noqa: E402


async def require_changed_notification(subscription, timeout=3):
    """PG polling can mask broken Pub/Sub, so a notification needs its own proof."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                message = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
                if message:
                    if message["data"] != b"changed":
                        raise RuntimeError("Redis Pub/Sub sent an unexpected notification")
                    return True
    except TimeoutError:
        raise RuntimeError("Redis Pub/Sub delivered no changed notification") from None


def require_live_prefix(state):
    if state["status"] != "running" or state["version_count"] != 0:
        raise RuntimeError("First SSE deltas must arrive while running, before formal module persistence")


async def main(one_click_only=False):
    settings = get_settings()
    validate_target(settings.database_url)
    if settings.environment != "development" or settings.ai_provider == "mock" or settings.task_queue_mode != "arq":
        raise ValueError("Canary requires development + PostgreSQL + ARQ + a real model")
    if settings.redis_url != "redis://127.0.0.1:56379/0":
        raise ValueError("Canary requires this project's local Docker Redis")
    identifier = uuid4().hex
    output = ROOT / ".cache/local-real-flow" / identifier
    output.mkdir(parents=True)
    evidence = {"provider": settings.ai_provider, "model": settings.llm_model, "runs": [], "exports": []}
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)

    def save():
        (output / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    async def snapshot(run_id):
        async with engine.connect() as conn:
            run = (await conn.execute(select(Run.__table__).where(Run.id == run_id))).mappings().one()
            calls = (await conn.execute(select(CallLog.__table__).where(CallLog.run_id == run_id))).mappings().all()
            events = (await conn.execute(select(Event.sequence, Event.event_type).where(Event.run_id == run_id)
                                         .order_by(Event.sequence))).all()
            return {"run_id": run_id, "draft_id": run["draft_id"], "status": run["status"],
                "sequence": run["sequence"], "queue": run["queue_name"], "job_id": run["job_id"],
                "version_count": await conn.scalar(select(func.count()).select_from(ModuleVersion)
                                                    .where(ModuleVersion.draft_id == run["draft_id"])),
                "error_code": run["error_code"], "event_counts": dict(Counter(r.event_type for r in events)),
                "calls": [{"model": r["model_name"], "provider": r["provider"], "status": r["status"],
                           "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"]} for r in calls]}

    try:
        async with asyncio.timeout(1200), httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=360) as http:
            health = await http.get("/health")
            health.raise_for_status()
            evidence["health"] = health.json()
            result = await http.post("/api/v1/auth/register", json={"email": f"live-{identifier}@example.test",
                "password": uuid4().hex + "!Live1", "name": "本地真实链路验收"})
            result.raise_for_status()
            headers = {"Authorization": "Bearer " + result.json()["data"]["access_token"]}

            async def data(method, path, payload=None):
                response = await http.request(method, "/api/v1" + path, headers=headers, json=payload)
                if response.is_error:
                    error = response.json().get("error", {})
                    raise RuntimeError(f"HTTP {response.status_code}: {path}: {error.get('code', 'UNKNOWN')}")
                return response.json()["data"]

            async def stream(run_id, label, disconnect=False):
                started = time.monotonic()
                received, first_delta, delta_count = [], None, 0
                subscription = redis.pubsub() if disconnect else None
                try:
                    if subscription:
                        await subscription.subscribe(f"lesson:run:{run_id}")
                    for attempt in range(2 if disconnect else 1):
                        extra = {"Last-Event-ID": str(received[-1])} if attempt else {}
                        async with http.stream("GET", f"/api/v1/generation-runs/{run_id}/events",
                                               headers={**headers, **extra}) as response:
                            response.raise_for_status()
                            kind = ""
                            async for line in response.aiter_lines():
                                if line.startswith("event: "):
                                    kind = line[7:]
                                if line.startswith("data: ") and kind != "heartbeat":
                                    event = json.loads(line[6:])
                                    received.append(event["sequence"])
                                    if kind == "content_delta":
                                        first_delta = first_delta or round((time.monotonic() - started) * 1000)
                                        delta_count += 1
                                    if kind in {"module_started", "stage_started"}:
                                        print(label, kind, "sequence", event["sequence"], flush=True)
                                    if disconnect and attempt == 0 and delta_count >= 2:
                                        break
                        if disconnect and attempt == 0:
                            before = await snapshot(run_id)
                            require_live_prefix(before)
                            await asyncio.sleep(1)
                            after = await snapshot(run_id)
                            evidence["disconnection"] = {"last_event_id": received[-1],
                                "before_status": before["status"], "before_version_count": before["version_count"],
                                "before_sequence": before["sequence"], "offline_sequence": after["sequence"]}
                            # A model can pause between tokens; the terminal readback below proves completion.
                            evidence["redis_notification"] = await require_changed_notification(subscription)
                    final = await snapshot(run_id)
                    final.update(label=label, elapsed_ms=round((time.monotonic() - started) * 1000),
                                 first_delta_ms=first_delta, delta_count=delta_count)
                    evidence["runs"].append(final)
                    save()
                    assert received == list(range(1, final["sequence"] + 1)), "SSE sequence gap or duplicate"
                    assert final["status"] == "completed", f"Run failed: {final['error_code']}"
                    assert final["event_counts"].get("run_completed") == 1
                    assert all(c["provider"] != "mock" and c["status"] == "success" for c in final["calls"])
                    if final["queue"] != "lesson:export":
                        assert first_delta is not None and delta_count > 0, "Generation did not stream content"
                        assert final["calls"], "Generation has no persisted model call receipt"
                    print(label, "completed", final["sequence"], "events", delta_count, "deltas", flush=True)
                    return final
                finally:
                    if subscription:
                        await subscription.aclose()

            body = {"request_id": identifier, "topic": "认识长方形与正方形", "school_stage": "primary",
                "grade": "grade_3", "subject": "math", "lesson_type": "new_lesson", "total_duration_min": 40,
                "requirements": "本地链路验收。内容简明可执行，适合小学三年级。教学过程包含3个环节，总时长40分钟；其余模块每个约100字。"}
            one = await data("POST", "/lesson-drafts/generations/one-click", body)
            print("one-click accepted", one["run_id"], flush=True)
            await stream(one["run_id"], "一键整课", disconnect=True)
            draft = await data("GET", f"/lesson-drafts/{one['draft_id']}")
            assert draft["status"] == "ready" and len(draft["modules"]) == 7
            assert all(m["current_version_id"] and m["teacher_view_content"].strip() for m in draft["modules"])
            evidence["one_click_draft"] = one["draft_id"]
            async with engine.connect() as conn:
                rows = (await conn.execute(select(ModuleVersion.id).where(ModuleVersion.draft_id == one["draft_id"]))).all()
                assert len(rows) == 7
            if one_click_only:
                evidence["scope"] = "one_click_streaming_recheck"
                evidence["completed"] = True
                save()
                print("ONE-CLICK STREAMING PASSED; evidence", output / "evidence.json", flush=True)
                return
            structured = await data("POST", "/lesson-drafts/generations/interactive", {**body, "request_id": identifier + "-structured"})
            await stream(structured["run_id"], "结构化首模块")
            path = f"/lesson-drafts/{structured['draft_id']}"

            async def confirm(module_id):
                current = await data("GET", path)
                module = next(m for m in current["modules"] if m["module_id"] == module_id)
                await data("POST", path + "/interactive/confirm", {"module_id": module_id,
                    "expected_content_version": current["content_version"],
                    "expected_current_module_version_id": module["current_version_id"],
                    "expected_module_config_version_id": current["module_config_version_id"]})

            current = await data("GET", path)
            await confirm(current["modules"][0]["module_id"])
            for module in current["modules"][1:]:
                stages = ["outline", "activities"] if module["module_type"] == "teaching_process" else [None]
                for stage in stages:
                    step = await data("POST", path + "/interactive/step", {"module_id": module["module_id"], "stage": stage})
                    await stream(step["run_id"], f"结构化:{module['display_name']}:{stage or '正文'}")
                    await confirm(module["module_id"])
            await data("POST", path + "/interactive/finalize", {})
            assert (await data("GET", path))["status"] == "ready"
            evidence["structured_draft"] = structured["draft_id"]
            for draft_id in [one["draft_id"], structured["draft_id"]]:
                preview = await data("POST", f"/lesson-drafts/{draft_id}/preview", {})
                assert preview["content"].strip()
                export = await data("POST", f"/lesson-drafts/{draft_id}/exports", {"format": "docx"})
                await stream(export["run_id"], "Word导出")
                record = await data("GET", f"/lesson-exports/{export['id']}")
                assert record["status"] == "success"
                download = await http.get(f"/api/v1/lesson-exports/{export['id']}/download", headers=headers)
                download.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    assert "word/document.xml" in archive.namelist()
                evidence["exports"].append({"draft_id": draft_id, "bytes": len(download.content), "docx_valid": True})
            evidence["completed"] = True
            save()
            print("ALL PASSED; evidence", output / "evidence.json", flush=True)
    finally:
        save()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true", required=True, help="Acknowledge real model token usage")
    parser.add_argument("--one-click-only", action="store_true", help="Only recheck live one-click streaming")
    args = parser.parse_args()
    asyncio.run(main(args.one_click_only))
