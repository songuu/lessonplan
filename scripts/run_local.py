"""Run the local API and seven independent ARQ Workers using the existing .env.

Start PostgreSQL/Redis and run Alembic first. No mock overrides or schema creation.
Ctrl+C (or `python scripts/run_local.py stop`) requests graceful shutdown.
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
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CACHE = ROOT / ".cache/local-runtime"
ROLES = {
    "one-click": "OneClickWorkerSettings", "structured": "StructuredWorkerSettings",
    "module-polish": "ModulePolishWorkerSettings", "export": "ExportWorkerSettings",
    "thumbnail": "ThumbnailWorkerSettings", "internal-review": "InternalReviewWorkerSettings",
    "maintenance": "MaintenanceWorkerSettings",
}


def restart_exited_workers(processes, launch, restart_counts, limit=3):
    """Replace only owned, exited Worker handles; never restart or signal by stale PID."""
    if processes["api"].poll() is not None:
        raise RuntimeError("API exited; stopping the local runtime group")
    restarted = []
    for role, process in list(processes.items()):
        if role == "api" or process.poll() is None:
            continue
        if restart_counts.get(role, 0) >= limit:
            raise RuntimeError(f"Worker {role} exhausted its restart budget; inspect local logs")
        processes[role] = launch(role)
        restart_counts[role] = restart_counts.get(role, 0) + 1
        restarted.append(role)
    return restarted


async def child(role, directory):
    import uvicorn
    from arq.worker import create_worker

    from app.core.config import get_settings
    from app.factory import create_app
    from app.tasks import workers

    settings = get_settings()
    if settings.task_queue_mode != "arq" or not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("Local runtime requires PostgreSQL + TASK_QUEUE_MODE=arq in .env")
    stop = directory / f"stop-{role}"
    if role == "api":
        server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=8000,
                                              access_log=False))
        task = asyncio.create_task(server.serve())
        try:
            while not stop.exists() and not task.done():
                await asyncio.sleep(.25)
        finally:
            server.should_exit = True
            await task
    else:
        worker = create_worker(getattr(workers, ROLES[role]))
        task = asyncio.create_task(worker.async_run())
        try:
            while not stop.exists() and not task.done():
                await asyncio.sleep(.25)
        finally:
            worker.handle_sig_wait_for_completion(signal.SIGTERM)
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await worker.close()


def supervisor():
    # The OS releases this lock on crashes; stale PID files never authorize killing a process.
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / "supervisor.lock").open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identifier = uuid4().hex
        directory = CACHE / identifier
        directory.mkdir()
        processes, logs, restart_counts = {}, [], {}
        current = CACHE / "current.json"
        try:
            # A Windows venv executable launches another interpreter. Own the actual process handle.
            executable = sys.executable
            environment = dict(os.environ)
            if os.name == "nt":
                executable = str(Path(sys.base_prefix) / "python.exe")
                environment["PYTHONPATH"] = os.pathsep.join([sysconfig.get_path("purelib"), str(ROOT)])
            def launch(role):
                log = (directory / f"{role}.log").open("ab")
                logs.append(log)
                return subprocess.Popen([executable, str(Path(__file__).resolve()), "--child", role,
                    "--directory", str(directory)], cwd=ROOT, env=environment, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def save_processes():
                temporary = directory / "current-next.json"
                temporary.write_text(json.dumps({"instance": identifier, "supervisor_pid": os.getpid(),
                    "processes": {role: proc.pid for role, proc in processes.items()},
                    "worker_restarts": restart_counts}), encoding="utf-8")
                temporary.replace(current)

            for role in ["api", *ROLES]:
                processes[role] = launch(role)
            save_processes()
            print("Local API + seven Workers started; status: http://127.0.0.1:8000/health", flush=True)
            while not (directory / "stop").exists():
                restarted = restart_exited_workers(processes, launch, restart_counts)
                if restarted:
                    save_processes()
                    print("Restarted Workers: " + ", ".join(restarted), flush=True)
                import time
                time.sleep(.5)
        except KeyboardInterrupt:
            pass
        finally:
            for role in processes:
                (directory / f"stop-{role}").touch()
            # Each child has its own run deadline. Do not interrupt a teacher's in-flight job.
            from app.core.config import get_settings
            for proc in processes.values():
                try:
                    proc.wait(timeout=get_settings().run_timeout_seconds + 60)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            for log in logs:
                log.close()
            if current.exists() and json.loads(current.read_text(encoding="utf-8"))["instance"] == identifier:
                current.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="start", choices=["start", "stop"])
    parser.add_argument("--child", choices=["api", *ROLES])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.child:
        if not args.directory or args.directory.resolve().parent != CACHE.resolve():
            parser.error("Child directory must be a local-runtime instance directory")
        asyncio.run(child(args.child, args.directory))
    elif args.command == "stop":
        current = json.loads((CACHE / "current.json").read_text(encoding="utf-8"))
        identifier = current["instance"]
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            parser.error("Invalid instance identifier")
        (CACHE / identifier / "stop").touch()
        print("Graceful shutdown requested; Docker services and data are retained")
    else:
        os.chdir(ROOT)
        supervisor()
