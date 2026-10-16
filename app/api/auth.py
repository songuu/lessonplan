"""认证和教师档案；注册永远不接受角色或租户。"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.errors import AppError
from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.db.models import Audit, TokenRevocation, User
from app.db.session import get_session

router = APIRouter(tags=["认证与档案"])


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("请输入有效邮箱地址")
        return value


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    profile: dict[str, Any] | None = None


def public_user(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "name": user.name, "tenant_id": user.tenant_id,
            "role": user.role, "is_active": user.is_active, "profile": user.profile,
            "preferences": user.preferences, "created_at": user.created_at.isoformat()}


@router.post("/auth/register", status_code=201)
async def register(body: Credentials, request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    if not request.app.state.settings.registration_enabled:
        raise AppError("REGISTRATION_DISABLED", "当前环境暂未开放注册", 403)
    existing = await session.scalar(select(User).where(User.email == body.email))
    if existing:
        raise AppError("EMAIL_ALREADY_REGISTERED", "该邮箱已注册", 409)
    password_hash = await run_in_threadpool(hash_password, body.password)
    user = User(email=body.email, password_hash=password_hash, name=body.name or "教师")
    session.add(user)
    try:
        await session.flush()
        session.add(Audit(user_id=user.id, action="auth.register", resource_id=user.id))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("EMAIL_ALREADY_REGISTERED", "该邮箱已注册", 409) from None
    return {"data": {"access_token": create_access_token(user, request.app.state.settings), "token_type": "bearer", "user": public_user(user)}}


@router.post("/auth/login")
async def login(body: Credentials, request: Request, session: Annotated[AsyncSession, Depends(get_session)]):
    user = await session.scalar(select(User).where(User.email == body.email))
    # Equal password-hash work for unknown users avoids exposing account existence through timing.
    if user is None:
        await run_in_threadpool(hash_password, body.password)
        raise AppError("INVALID_CREDENTIALS", "邮箱或密码错误", 401)
    if not await run_in_threadpool(verify_password, body.password, user.password_hash) or not user.is_active:
        raise AppError("INVALID_CREDENTIALS", "邮箱或密码错误", 401)
    session.add(Audit(user_id=user.id, action="auth.login", resource_id=user.id))
    await session.commit()
    return {"data": {"access_token": create_access_token(user, request.app.state.settings), "token_type": "bearer", "user": public_user(user)}}


@router.post("/auth/logout")
async def logout(request: Request, user: Annotated[User, Depends(get_current_user)], session: Annotated[AsyncSession, Depends(get_session)]):
    claims = request.state.auth_claims
    session.add(TokenRevocation(jti=claims["jti"], expires_at=datetime.fromtimestamp(claims["exp"], UTC)))
    session.add(Audit(user_id=user.id, action="auth.logout", resource_id=user.id))
    await session.commit()
    return {"data": {"logged_out": True}}


@router.get("/me")
async def me(user: Annotated[User, Depends(get_current_user)]):
    return {"data": public_user(user)}


@router.patch("/me")
async def update_profile(body: ProfilePatch, user: Annotated[User, Depends(get_current_user)], session: Annotated[AsyncSession, Depends(get_session)]):
    if body.name is not None:
        user.name = body.name
    if body.profile is not None:
        if len(str(body.profile)) > 10000:
            raise AppError("INVALID_PROFILE", "教师档案内容过长", 422)
        user.profile = {**user.profile, **body.profile}
    await session.commit()
    return {"data": public_user(user)}
