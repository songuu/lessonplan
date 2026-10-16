"""Explicit local maintenance. Never seeds a default account or password."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import User
from app.db.session import Database


async def create_admin(args):
    password = getpass.getpass("管理员密码（至少12字符）: ")
    if len(password) < 12 or password != getpass.getpass("再次输入: "):
        raise ValueError("密码不一致或长度不足")
    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            email = args.email.strip().lower()
            user = await session.scalar(select(User).where(User.email == email).with_for_update())
            if user and not args.promote:
                raise ValueError("账号已存在；确需提升权限时显式提供 --promote")
            if user:
                user.role, user.password_hash, user.is_active = "admin", hash_password(password), True
            else:
                session.add(User(email=email, name=args.name, password_hash=hash_password(password), role="admin"))
            await session.commit()
        print("管理员账号已保存")
    finally:
        await database.engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", default="管理员")
    admin.add_argument("--promote", action="store_true")
    sub.add_parser("check-config")
    args = parser.parse_args()
    try:
        if args.command == "create-admin":
            asyncio.run(create_admin(args))
        else:
            settings = get_settings()
            print({"environment": settings.environment, "queue": settings.task_queue_mode,
                   "ai_provider": settings.ai_provider, "storage": settings.storage_backend})
    except Exception as exc:
        # DSNs and pydantic exception strings can contain credentials.
        print(f"操作失败：{type(exc).__name__}；请核对配置和输入", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
