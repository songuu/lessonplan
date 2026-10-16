"""受控字典和仅影响下一次创建的个人模块偏好。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.catalog import default_modules, dictionaries, normalize_modules
from app.core.security import get_current_user
from app.db.models import User
from app.db.session import get_session

router = APIRouter(tags=["字典与模块配置"])


class ModulePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modules: list[dict[str, Any]]


@router.get("/lesson-dictionaries")
async def lesson_dictionaries():
    return {"data": dictionaries()}


@router.get("/profile-options")
async def profile_options():
    result = dictionaries()
    result.pop("modules")
    return {"data": result}


@router.get("/me/lesson-module-config")
async def get_module_preferences(user: Annotated[User, Depends(get_current_user)]):
    return {"data": {"modules": user.preferences.get("modules") or default_modules()}}


@router.put("/me/lesson-module-config")
async def save_module_preferences(body: ModulePreferences, user: Annotated[User, Depends(get_current_user)], session: Annotated[AsyncSession, Depends(get_session)]):
    modules = normalize_modules(body.modules)
    user.preferences = {**user.preferences, "modules": modules}
    await session.commit()
    return {"data": {"modules": modules}}
