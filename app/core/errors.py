"""只向客户端公开受控错误；内部异常由服务端日志记录。"""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}, "request_id": request_id})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def protocol_error(request: Request, exc: HTTPException):
        messages = {400: "请求格式无效", 404: "接口不存在", 405: "请求方法不支持", 413: "请求内容超过大小限制"}
        return error_response(request, f"HTTP_{exc.status_code}", messages.get(exc.status_code, "请求未能完成"), exc.status_code)

    @app.exception_handler(AppError)
    async def controlled_error(request: Request, exc: AppError):
        return error_response(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Validation contexts can contain passwords or uploaded content; expose only field paths.
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        return error_response(request, "VALIDATION_ERROR", "请求参数不符合要求：" + "、".join(fields), 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled application error type=%s request_id=%s", type(exc).__name__, getattr(request.state, "request_id", "unknown"))
        return error_response(request, "INTERNAL_ERROR", "服务暂时不可用，请稍后重试", 500)
