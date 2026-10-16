"""短期 JWT + 数据库撤销；每次请求回读用户状态和角色。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import TokenRevocation, User
from app.db.session import get_session

password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password, password_hash)
    except (ValueError, TypeError):
        return False


def create_access_token(user: User, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode({
        "sub": user.id, "jti": str(uuid4()), "iat": now, "nbf": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes), "iss": settings.jwt_issuer,
    }, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError("AUTHENTICATION_REQUIRED", "请先登录", 401)
    settings = request.app.state.settings
    try:
        claims = jwt.decode(
            credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer, options={"require": ["sub", "jti", "iat", "nbf", "exp", "iss"]},
        )
    except InvalidTokenError:
        raise AppError("INVALID_TOKEN", "登录凭证无效或已过期，请重新登录", 401) from None
    if not isinstance(claims["sub"], str) or not isinstance(claims["jti"], str):
        raise AppError("INVALID_TOKEN", "登录凭证无效，请重新登录", 401)
    if await session.get(TokenRevocation, claims["jti"]):
        raise AppError("TOKEN_REVOKED", "登录凭证已退出，请重新登录", 401)
    user = await session.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise AppError("ACCOUNT_UNAVAILABLE", "账号不可用", 401)
    request.state.auth_claims = claims
    return user


current_user = get_current_user


async def admin_user(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != "admin":
        raise AppError("FORBIDDEN", "当前账号没有管理员权限", 403)
    return user
