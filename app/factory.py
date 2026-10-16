import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db.session import Database


def create_app(settings=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        await app.state.dispatcher.connect()
        # Schema creation is exclusively Alembic, never application startup.
        if settings.task_queue_mode == "inline":
            await app.state.dispatcher.reconcile()
        yield
        await app.state.dispatcher.close()
        await app.state.db.engine.dispose()

    app = FastAPI(title="教案助手 API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = Database(settings)
    app.state.admission_lock = asyncio.Lock()
    from app.ai.runtime import LessonRuntime
    from app.integrations.storage import create_storage
    from app.tasks.queue import Dispatcher
    app.state.runtime = LessonRuntime(settings)
    app.state.storage = create_storage(settings)
    app.state.dispatcher = Dispatcher(app)
    install_error_handlers(app)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if len(supplied) <= 100 and supplied.isascii() and supplied.isprintable() else str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    from app.core.request_boundary import RequestBoundaryMiddleware
    app.add_middleware(RequestBoundaryMiddleware, settings=settings)
    # CORS must cover early authentication/size errors as well as routed responses.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Content-Disposition"])

    from app.api import admin, auth, catalog, knowledge, lessons
    for module in (auth, catalog, lessons, knowledge, admin):
        app.include_router(module.router, prefix="/api/v1")

    @app.get("/live")
    async def live():
        return {"status": "alive"}

    async def dependencies():
        checks = {"database": False, "redis": None if settings.task_queue_mode == "inline" else False}
        try:
            async with app.state.db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                # A reachable empty database is not schema readiness.
                await conn.execute(text("SELECT id FROM users LIMIT 1"))
            checks["database"] = True
        except Exception:
            pass
        if app.state.dispatcher.redis:
            try:
                checks["redis"] = bool(await app.state.dispatcher.redis.ping())
            except Exception:
                pass
        return checks

    @app.get("/ready")
    async def ready():
        checks = await dependencies()
        available = checks["database"] and (settings.task_queue_mode == "inline" or checks["redis"])
        return JSONResponse({"status": "ready" if available else "unavailable", "checks": checks},
                            status_code=200 if available else 503)

    @app.get("/health")
    async def health():
        from app.tasks.queue import QUEUES
        checks = await dependencies()
        workers = {}
        if settings.task_queue_mode == "arq":
            for queue in QUEUES:
                key = f"lesson:{queue}"
                try:
                    redis = app.state.dispatcher.redis
                    workers[queue] = {"healthy": bool(redis and await redis.exists(f"{key}:health")),
                                      "depth": await redis.zcard(key) if redis else None}
                except Exception:
                    workers[queue] = {"healthy": False, "depth": None}
        healthy = checks["database"] and (settings.task_queue_mode == "inline" or checks["redis"]) and all(w["healthy"] for w in workers.values())
        return JSONResponse({"status": "healthy" if healthy else "degraded", "mode": settings.task_queue_mode,
            "ai_provider": settings.ai_provider, "checks": checks, "workers": workers}, status_code=200 if healthy else 503)

    dist = Path(settings.frontend_dist)
    if dist.is_dir() and (dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            if path.startswith("api/"):
                return JSONResponse({"error": {"code": "NOT_FOUND", "message": "接口不存在"}}, status_code=404)
            return FileResponse(dist / "index.html")
    return app
