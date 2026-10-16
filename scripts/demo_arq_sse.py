"""Opt-in multiprocess demonstration using a dedicated loopback lesson_test database.

Uses real API/ARQ/PostgreSQL/Redis processes and an explicitly slowed local mock LLM.
Never flushes Redis, deletes business data, or changes the normally running application.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from arq.connections import RedisSettings, create_pool  # noqa: E402
from arq.jobs import Job  # noqa: E402
from arq.worker import Worker  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.ai.gateway import LLMGateway  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.models import CallLog, Event, ModuleVersion, Outbox, Run  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.factory import create_app  # noqa: E402
from app.tasks.workers import (  # noqa: E402
    OneClickWorkerSettings,
    StructuredWorkerSettings,
    execute_run,
    shutdown,
    startup,
)
from scripts.test_external_runtime import endpoints, free_port, prepare_database  # noqa: E402


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def redis_identity(url):
    parsed = urlsplit(url)
    return {"host": parsed.hostname, "port": parsed.port, "database": parsed.path, "initial_keys": 0}


def stop_owned_child(process):
    """Only terminate the directly owned Popen handle, never a PID from a file."""
    method = "cooperative"
    for timeout, next_method in [(15, "terminate"), (5, "kill"), (5, None)]:
        try:
            process.wait(timeout=timeout)
            return {"exited": True, "exit_code": process.returncode, "shutdown": method}
        except subprocess.TimeoutExpired:
            if next_method is None:
                raise
            with contextlib.suppress(ProcessLookupError):
                getattr(process, next_method)()
            method = next_method


def settings():
    database, redis = endpoints()
    return Settings(_env_file=None, environment="test", database_url=database, redis_url=redis,
        task_queue_mode="arq", ai_provider="mock", storage_backend="local",
        storage_root=os.environ["STORAGE_ROOT"], frontend_dist=ROOT / ".cache/no-demo-frontend",
        run_timeout_seconds=60)


async def child(role, output, port):
    config = settings()
    assert config.ai_provider == "mock" and config.storage_backend == "local"
    identity = {"pid": os.getpid(), "role": role, "provider": "mock"}
    stopped = output / f"stop-{role}"
    if role == "api":
        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False))
        write_json(output / f"{role}.json", identity)
        task = asyncio.create_task(server.serve())
        try:
            while not stopped.exists():
                if task.done():
                    await task
                    return
                await asyncio.sleep(.05)
        finally:
            server.should_exit = True
            await task
        return

    class SlowMockGateway(LLMGateway):
        async def complete(self, messages, **options):
            original = options.get("on_delta")

            async def paced_delta(text):
                # A bounded mock delay makes the actual disconnection interval observable.
                await asyncio.sleep(.06)
                await original(text)

            if original:
                options["on_delta"] = paced_delta
            with (output / f"calls-{role}.jsonl").open("a", encoding="utf-8") as trace:
                trace.write(json.dumps({"pid": os.getpid(), "route": options["route"]}) + "\n")
            return await super().complete(messages, **options)

    async def demo_startup(ctx):
        await startup(ctx)
        ctx["app"].state.runtime.gateway = SlowMockGateway(config)
        write_json(output / f"{role}.json", identity)

    worker_class = OneClickWorkerSettings if role == "one-click" else StructuredWorkerSettings
    worker = Worker([execute_run], redis_settings=RedisSettings.from_dsn(config.redis_url),
        queue_name=worker_class.queue_name, health_check_key=worker_class.health_check_key,
        max_jobs=worker_class.max_jobs, max_tries=1, retry_jobs=False, job_timeout=90,
        on_startup=demo_startup, on_shutdown=shutdown, poll_delay=.05,
        health_check_interval=.5, handle_signals=True, log_results=False)
    task = asyncio.create_task(worker.async_run())
    try:
        while not stopped.exists():
            if task.done():
                await task
                return
            await asyncio.sleep(.05)
    finally:
        # Use the same portable shutdown path as the real worker entry point.
        worker.handle_sig(signal.SIGTERM)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await worker.close()


async def demonstrate():
    database_url, redis_url = endpoints()
    identifier = uuid4().hex
    output = ROOT / ".cache/arq-sse-demo" / identifier
    output.mkdir(parents=True)
    # Children and Alembic must never inherit a real model/storage route from the shell.
    os.environ.update(ENVIRONMENT="test", AI_PROVIDER="mock", STORAGE_BACKEND="local",
        DATABASE_URL=database_url, REDIS_URL=redis_url, TASK_QUEUE_MODE="arq",
        STORAGE_ROOT=str(output / "storage"), FRONTEND_DIST=str(output / "no-frontend"))
    evidence = {"output": str(output), "ai_provider": "explicit_local_mock",
        "driver_pid": os.getpid(), "steps": [], "children": {},
        "limits": ["No real LLM/COS calls", "No OS worker crash or module-checkpoint resume demonstrated"]}
    evidence["database"] = await prepare_database(database_url)
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    if await pool.dbsize():
        await pool.aclose()
        raise ValueError("Dedicated Redis DB must be empty; refusing to flush or reuse it")
    evidence["redis"] = redis_identity(redis_url)
    db = Database(settings())
    children, log_files = {}, []
    pubsub = None
    port = free_port()

    def launch(role):
        log = (output / f"{role}.log").open("wb")
        log_files.append(log)
        environment = dict(os.environ)
        executable = sys.executable
        if os.name == "nt":
            # Windows venv python.exe is a launcher. Use its actual interpreter so
            # Popen owns the API/Worker handle directly and can safely stop it.
            executable = str(Path(sys.base_prefix) / "python.exe")
            environment["PYTHONPATH"] = os.pathsep.join([sysconfig.get_path("purelib"), str(ROOT)])
        process = subprocess.Popen([executable, str(Path(__file__).resolve()), "--child", role,
            "--output", str(output), "--port", str(port)], cwd=ROOT, env=environment,
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        children[role] = process

    async def snapshot(run_id):
        async with db.session_factory() as session:
            run = await session.get(Run, run_id)
            rows = (await session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))).all()
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == run_id, Outbox.event_type == "dispatch_run"))
            return {"run_id": run.id, "status": run.status, "sequence": run.sequence,
                "event_sequences": [row.sequence for row in rows],
                "terminal_events": sum(row.event_type in {"run_completed", "run_failed", "run_cancelled"} for row in rows),
                "versions": await session.scalar(select(func.count()).select_from(ModuleVersion).where(ModuleVersion.draft_id == run.draft_id)),
                "calls": await session.scalar(select(func.count()).select_from(CallLog).where(CallLog.run_id == run_id)),
                "job_id": run.job_id, "queue": run.queue_name, "outbox": intent.status if intent else None}

    try:
        launch("api")
        # Read timeouts alone cannot bound an SSE stream which keeps heartbeating.
        async with asyncio.timeout(90), httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
            async with asyncio.timeout(20):
                while True:
                    if children["api"].poll() is not None:
                        raise RuntimeError("Demo API exited; inspect its local log")
                    try:
                        if (await http.get("/ready")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(.05)
            response = await http.post("/api/v1/auth/register", json={"email": f"demo-{identifier}@example.test",
                "password": uuid4().hex + "!Demo1", "name": "链路演示教师"})
            response.raise_for_status()
            headers = {"Authorization": "Bearer " + response.json()["data"]["access_token"]}
            body = {"request_id": identifier, "topic": "分数的初步认识", "school_stage": "primary",
                "grade": "grade_3", "subject": "math", "lesson_type": "new_lesson", "total_duration_min": 40}
            started = time.monotonic()
            response = await http.post("/api/v1/lesson-drafts/generations/one-click", headers=headers, json=body)
            response.raise_for_status()
            item = response.json()["data"]
            elapsed = round((time.monotonic() - started) * 1000)
            queued = await snapshot(item["run_id"])
            assert queued["status"] == "queued" and queued["versions"] == 0 and queued["outbox"] == "published"
            assert (await Job(queued["job_id"], pool, _queue_name=queued["queue"]).status()).value == "queued"
            evidence["steps"].append({"step": "HTTP返回时还没有Worker进程", "http_ms": elapsed, **queued})
            same = await http.post("/api/v1/lesson-drafts/generations/one-click", headers=headers, json=body)
            assert same.json()["data"] == item
            evidence["idempotency_same_request_same_run"] = True
            launch("one-click")
            launch("structured")
            channel = f"lesson:run:{item['run_id']}"
            pubsub = pool.pubsub()
            await pubsub.subscribe(channel)
            received = []
            delta_count = 0
            async with http.stream("GET", f"/api/v1/generation-runs/{item['run_id']}/events", headers=headers) as stream:
                stream.raise_for_status()
                kind = ""
                async for line in stream.aiter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    if line.startswith("data: ") and kind != "heartbeat":
                        event = json.loads(line[6:])
                        received.append({"sequence": event["sequence"], "event": kind})
                        delta_count += int(kind == "content_delta")
                        if delta_count == 2:
                            break
            before = await snapshot(item["run_id"])
            assert before["status"] == "running"
            last_id = received[-1]["sequence"]
            evidence["steps"].append({"step": "客户端主动关闭SSE", "last_event_id": last_id, **before})
            await asyncio.sleep(.65)
            after = await snapshot(item["run_id"])
            assert after["sequence"] > before["sequence"]
            evidence["steps"].append({"step": "SSE断开期间数据库事件继续增加", **after})
            for _ in range(10):
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                if message:
                    assert message["data"] == b"changed"
                    evidence["pubsub"] = {"channel": channel, "payload": "changed",
                        "postgresql_sequence_when_read": (await snapshot(item["run_id"]))["sequence"]}
                    break
            assert "pubsub" in evidence
            replayed = []
            async with http.stream("GET", f"/api/v1/generation-runs/{item['run_id']}/events",
                headers={**headers, "Last-Event-ID": str(last_id)}) as stream:
                stream.raise_for_status()
                kind = ""
                async for line in stream.aiter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    if line.startswith("data: ") and kind != "heartbeat":
                        event = json.loads(line[6:])
                        replayed.append({"sequence": event["sequence"], "event": kind})
            final = await snapshot(item["run_id"])
            assert final["status"] == "completed" and final["versions"] == 7 and final["terminal_events"] == 1
            assert replayed[0]["sequence"] == last_id + 1
            assert [r["sequence"] for r in received + replayed] == list(range(1, final["sequence"] + 1))
            evidence["steps"].append({"step": "携带Last-Event-ID重连并收齐正式结果",
                "replay_from": replayed[0]["sequence"], "replayed_count": len(replayed), **final})
            evidence["one_click_sse"] = {"before_disconnect": received, "after_reconnect": replayed}

            async def data(method, path, payload=None):
                result = await http.request(method, "/api/v1" + path, headers=headers, json=payload)
                result.raise_for_status()
                return result.json()["data"]

            async def wait(run_id):
                async with asyncio.timeout(30):
                    while True:
                        state = await snapshot(run_id)
                        if state["status"] in {"completed", "failed", "cancelled"}:
                            assert state["status"] == "completed", state
                            return state
                        await asyncio.sleep(.05)

            structured = await data("POST", "/lesson-drafts/generations/interactive", {**body,
                "request_id": identifier + "-structured", "modules": [
                    {"module_type": "teaching_objectives", "display_name": "教学目标", "enabled": True},
                    {"module_type": "teaching_process", "display_name": "教学过程", "enabled": True}]})
            structured_steps = [{"step": "蓝图规划+教学目标", **await wait(structured["run_id"])}]
            path = f"/lesson-drafts/{structured['draft_id']}"

            async def confirm(index):
                draft = await data("GET", path)
                module = draft["modules"][index]
                await data("POST", path + "/interactive/confirm", {"module_id": module["module_id"],
                    "expected_content_version": draft["content_version"],
                    "expected_current_module_version_id": module["current_version_id"],
                    "expected_module_config_version_id": draft["module_config_version_id"]})
                return module["module_id"]

            await confirm(0)
            draft = await data("GET", path)
            process_id = draft["modules"][1]["module_id"]
            for stage, label in [("outline", "复用蓝图+教学过程大纲"), ("activities", "复用蓝图+三个课堂环节")]:
                step = await data("POST", path + "/interactive/step", {"module_id": process_id, "stage": stage})
                structured_steps.append({"step": label, **await wait(step["run_id"])})
                await confirm(1)
            await data("POST", path + "/interactive/finalize", {})
            assert structured_steps[0]["calls"] == 2 and structured_steps[1]["calls"] == 1 and structured_steps[2]["calls"] == 3
            evidence["structured_steps"] = structured_steps
            evidence["structured_final_status"] = (await data("GET", path))["status"]
            for role in children:
                identity = json.loads((output / f"{role}.json").read_text(encoding="utf-8"))
                assert identity["pid"] == children[role].pid
                evidence["children"][role] = {**identity, "owned_pid": children[role].pid}
            assert len({evidence["driver_pid"], *(x["pid"] for x in evidence["children"].values())}) == 4
            evidence["separate_processes_verified"] = True
    finally:
        for role in children:
            (output / f"stop-{role}").touch()
        cleanup = {}
        for role, process in children.items():
            try:
                cleanup[role] = await asyncio.to_thread(stop_owned_child, process)
            except Exception as exc:
                cleanup[role] = {"exited": False, "owned_pid": process.pid, "error": type(exc).__name__}
        evidence["child_cleanup"] = cleanup
        for log in log_files:
            log.close()
        cleanup_errors = []
        for label, close in [*([("pubsub", pubsub.aclose)] if pubsub else []),
                             ("database", db.engine.dispose), ("redis", pool.aclose)]:
            try:
                async with asyncio.timeout(3):
                    await close()
            except Exception as exc:
                cleanup_errors.append({"resource": label, "error": type(exc).__name__})
        evidence["cleanup_errors"] = cleanup_errors
        write_json(output / "evidence.json", evidence)
        if cleanup_errors or not all(state["exited"] and state["exit_code"] == 0 for state in cleanup.values()):
            raise RuntimeError(f"A demo child did not exit cleanly; inspect {output}")
    print(json.dumps({"evidence": str(output / "evidence.json"), "api_pid": evidence["children"]["api"]["pid"],
        "worker_pids": [evidence["children"][role]["pid"] for role in ["one-click", "structured"]],
        "steps": [(step["step"], step["status"], step["sequence"]) for step in evidence["steps"]],
        "structured_llm_calls": [s["calls"] for s in evidence["structured_steps"]], "cleanup": evidence["child_cleanup"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=["api", "one-click", "structured"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.child:
        if not args.output or not args.output.resolve().is_relative_to((ROOT / ".cache/arq-sse-demo").resolve()):
            parser.error("Child output must belong to this workspace's .cache/arq-sse-demo")
        asyncio.run(child(args.child, args.output, args.port))
    else:
        asyncio.run(demonstrate())
