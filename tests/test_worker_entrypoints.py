import pytest
from arq.worker import create_worker, get_kwargs

from app.tasks import workers

SETTINGS = [getattr(workers, name) for name in (
    "OneClickWorkerSettings", "StructuredWorkerSettings", "ModulePolishWorkerSettings",
    "ExportWorkerSettings", "ThumbnailWorkerSettings", "InternalReviewWorkerSettings", "MaintenanceWorkerSettings",
)]


@pytest.mark.parametrize("settings", SETTINGS)
async def test_real_arq_entrypoints_include_shared_configuration(settings):
    options = get_kwargs(settings)
    assert options.get("functions") == [workers.execute_run]
    assert options.get("on_startup") is workers.startup
    assert options.get("on_shutdown") is workers.shutdown
    assert options.get("redis_settings") is workers.BaseWorker.redis_settings
    assert options.get("max_tries") == 1
    assert options.get("log_results") is False
    worker = create_worker(settings)
    assert worker.queue_name == settings.queue_name
    assert worker._pool is None  # Construction must not contact Redis or any real model.
