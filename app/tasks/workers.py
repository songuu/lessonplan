"""Seven isolated ARQ worker entry points, using the same service owner as HTTP."""

import asyncio

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings


async def startup(ctx):
    from app.factory import create_app
    app = create_app(get_settings())
    ctx["app"] = app
    await app.state.execution_logs.start()
    app.state.observer.start()
    await app.state.dispatcher.connect()


async def shutdown(ctx):
    await ctx["app"].state.dispatcher.close()
    settings = ctx["app"].state.settings
    await ctx["app"].state.execution_logs.close(settings.obs_shutdown_timeout_seconds)
    await asyncio.to_thread(ctx["app"].state.observer.close, timeout=settings.obs_shutdown_timeout_seconds)
    await ctx["app"].state.db.engine.dispose()


async def execute_run(ctx, run_id, expected_epoch=0):
    await ctx["app"].state.dispatcher._execute(run_id, expected_epoch)


async def maintenance(ctx):
    from app.services.recovery import recovery_log
    try:
        await ctx["app"].state.dispatcher.reconcile()
    except Exception:
        # Durable intents remain due; do not leak raw database/queue exceptions through ARQ logs.
        recovery_log("recovery_maintenance_unavailable", code="MAINTENANCE_UNAVAILABLE")


class BaseWorker:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # ARQ CLI/create_worker reads only the concrete class __dict__, not its MRO.
        # Materialize shared settings so CLI, Docker and local entry points agree.
        for name, value in BaseWorker.__dict__.items():
            if not name.startswith("_") and name not in cls.__dict__:
                setattr(cls, name, value)

    functions = [execute_run]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Leave time for the service deadline to persist its terminal state before ARQ cancels the task.
    job_timeout = get_settings().run_timeout_seconds + 30
    job_completion_wait = get_settings().run_timeout_seconds + 40
    health_check_interval = 15
    # Maintenance owns business retries through new fenced epoch IDs.
    max_tries = 1
    keep_result = 3600
    log_results = False
    allow_abort_jobs = True


class OneClickWorkerSettings(BaseWorker):
    queue_name = "lesson:one-click"
    health_check_key = "lesson:one-click:health"
    max_jobs = 8


class StructuredWorkerSettings(BaseWorker):
    queue_name = "lesson:structured"
    health_check_key = "lesson:structured:health"
    max_jobs = 3


class ModulePolishWorkerSettings(BaseWorker):
    queue_name = "lesson:module-polish"
    health_check_key = "lesson:module-polish:health"
    max_jobs = 6


class ExportWorkerSettings(BaseWorker):
    queue_name = "lesson:export"
    health_check_key = "lesson:export:health"
    max_jobs = 4


class ThumbnailWorkerSettings(BaseWorker):
    queue_name = "lesson:thumbnail"
    health_check_key = "lesson:thumbnail:health"
    max_jobs = 4


class InternalReviewWorkerSettings(BaseWorker):
    queue_name = "lesson:internal-review"
    health_check_key = "lesson:internal-review:health"
    max_jobs = 2


class MaintenanceWorkerSettings(BaseWorker):
    queue_name = "lesson:maintenance"
    health_check_key = "lesson:maintenance:health"
    max_jobs = 2
    cron_jobs = [cron(maintenance, second=set(range(0, 60, 2)), unique=True, run_at_startup=True)]
