"""Authenticate uploads and bound request bytes before multipart can spool to disk."""

import asyncio
import re
from uuid import uuid4

from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.errors import AppError
from app.core.security import get_current_user


class RequestBoundaryMiddleware:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.uploads = asyncio.Semaphore(settings.max_parallel_uploads)
        self.requests = asyncio.Semaphore(32)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        request = Request(scope)
        upload = scope["method"] == "POST" and (scope["path"].rstrip("/") == "/api/v1/personal-lessons"
            or bool(re.fullmatch(r"/api/v1/knowledge-bases/[^/]+/documents/?", scope["path"])))
        limit = self.settings.upload_max_bytes + 8192 if upload else 1024 * 1024

        async def reject(code, message, status):
            response = JSONResponse({"error": {"code": code, "message": message},
                "request_id": str(uuid4())}, status_code=status)
            await response(scope, receive, send)

        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return await reject("INVALID_CONTENT_LENGTH", "请求长度无效", 400)
        if length < 0 or length > limit:
            return await reject("REQUEST_TOO_LARGE", "请求内容超过大小限制", 413)
        async with self.uploads if upload else self.requests:
            if upload:
                header = request.headers.get("authorization", "")
                scheme, _, token = header.partition(" ")
                credentials = HTTPAuthorizationCredentials(scheme=scheme, credentials=token) if scheme and token else None
                try:
                    async with request.app.state.db.session_factory() as session:
                        await get_current_user(request, session, credentials)
                except AppError as exc:
                    return await reject(exc.code, exc.message, exc.status_code)
            pieces, total = [], 0
            try:
                async with asyncio.timeout(self.settings.request_body_timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        part = message.get("body", b"")
                        total += len(part)
                        if total > limit:
                            return await reject("REQUEST_TOO_LARGE", "请求内容超过大小限制", 413)
                        pieces.append(part)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject("REQUEST_BODY_TIMEOUT", "上传或请求接收超时，请重试", 408)
            body, delivered = b"".join(pieces), False
            del pieces

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()
            return await self.app(scope, bounded_receive, send)
