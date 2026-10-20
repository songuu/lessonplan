"""Prepare private configuration for the isolated, local Langfuse stack.

Never changes the lesson application's .env or starts Docker without --start.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "lesson-assistant-observability"
BASE_URL = "http://127.0.0.1:3100"
_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9][A-Za-z0-9.-]{0,188}\.[A-Za-z]{2,32}\Z")


class SetupError(Exception):
    """An operator-facing message containing no credentials or raw errors."""


@dataclass(frozen=True)
class ConfigurationFiles:
    stack_env: Path
    app_env: Path
    dry_run: bool = False


def configuration_paths(root: Path) -> ConfigurationFiles:
    root = root.resolve()
    if not root.is_dir():
        raise SetupError("工作区路径不存在或不是目录。")
    cache = root / ".cache"
    if cache.is_symlink() or not cache.resolve().is_relative_to(root):
        raise SetupError("配置路径必须位于当前工作区的 .cache 目录内，不能通过链接重定向。")
    if cache.exists() and not cache.is_dir():
        raise SetupError("配置路径 .cache 已存在且不是目录。")
    result = ConfigurationFiles(cache / "langfuse.env", cache / "langfuse-app.env")
    for target in (result.stack_env, result.app_env):
        if target.exists() or target.is_symlink():
            raise SetupError("Langfuse 配置文件已存在；为保留已有密钥，本命令拒绝覆盖。")
    return result


def _configuration(*, headless: bool, admin_email: str | None) -> tuple[dict[str, str], dict[str, str]]:
    if admin_email and not headless:
        raise SetupError("--admin-email 仅能与 --headless 一起使用。")
    email = admin_email or "admin@lesson-assistant.local"
    if headless and not _EMAIL.fullmatch(email):
        raise SetupError("管理员邮箱格式无效；仅接受无空白、无控制字符的常规邮箱。")
    stack = {
        "LANGFUSE_PG_PASSWORD": secrets.token_urlsafe(36),
        "LANGFUSE_CLICKHOUSE_PASSWORD": secrets.token_urlsafe(36),
        "LANGFUSE_REDIS_AUTH": secrets.token_urlsafe(36),
        "LANGFUSE_MINIO_USER": f"lf{secrets.token_hex(12)}",
        "LANGFUSE_MINIO_PASSWORD": secrets.token_urlsafe(36),
        "LANGFUSE_SALT": secrets.token_urlsafe(36),
        "LANGFUSE_NEXTAUTH_SECRET": secrets.token_urlsafe(36),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_AUTH_DISABLE_SIGNUP": "true" if headless else "false",
    }
    app = {
        "LANGFUSE_ENABLED": "true" if headless else "false",
        "LANGFUSE_BASE_URL": BASE_URL,
        "LANGFUSE_PROJECT": "lesson-assistant",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "OBS_DEPLOYMENT_ID": f"lesson-assistant-{uuid4()}",
        "OBS_CAPTURE_MODE": "metadata",
        "OBS_TRACE_SAMPLE_RATE": "1",
    }
    if headless:
        stack.update({
            "LANGFUSE_INIT_ORG_ID": str(uuid4()),
            "LANGFUSE_INIT_ORG_NAME": "LessonAssistant",
            "LANGFUSE_INIT_PROJECT_ID": str(uuid4()),
            "LANGFUSE_INIT_PROJECT_NAME": app["LANGFUSE_PROJECT"],
            "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": f"pk-lf-{uuid4()}",
            "LANGFUSE_INIT_PROJECT_SECRET_KEY": f"sk-lf-{secrets.token_urlsafe(36)}",
            "LANGFUSE_INIT_USER_EMAIL": email,
            "LANGFUSE_INIT_USER_NAME": "Administrator",
            "LANGFUSE_INIT_USER_PASSWORD": secrets.token_urlsafe(36),
        })
        app["LANGFUSE_PUBLIC_KEY"] = stack["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
        app["LANGFUSE_SECRET_KEY"] = stack["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
    return stack, app


def _env_text(values: dict[str, str], description: str) -> str:
    # Generated values and validated email contain no dotenv interpolation or
    # quoting syntax. Unquoted headless values are required by Langfuse.
    return f"# {description}; contains secrets, do not commit or print.\n" + "".join(
        f"{key}={value}\n" for key, value in values.items())


def prepare_configuration(root: Path, *, headless=False, admin_email=None, dry_run=False) -> ConfigurationFiles:
    files = configuration_paths(root)
    # Validate even during dry-run, before creating directories or files.
    stack, app = _configuration(headless=headless, admin_email=admin_email)
    if dry_run:
        return ConfigurationFiles(files.stack_env, files.app_env, dry_run=True)
    created = []
    try:
        files.stack_env.parent.mkdir(mode=0o700, exist_ok=True)
        for target, values, description in (
            (files.stack_env, stack, "Langfuse infrastructure and optional initial admin"),
            (files.app_env, app, "Lesson application Langfuse settings"),
        ):
            # Exclusive creation prevents a concurrent setup from replacing a
            # key pair after the earlier existence check has passed.
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(target)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(_env_text(values, description))
    except OSError as failure:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        if isinstance(failure, FileExistsError):
            raise SetupError("Langfuse 配置文件已存在；拒绝覆盖并保留已有文件。") from None
        raise SetupError("Langfuse 配置写入失败；未覆盖已有文件。请检查目录写入权限。") from None
    return files


def compose_command(root: Path, files: ConfigurationFiles) -> list[str]:
    return ["docker", "compose", "--project-name", PROJECT, "--env-file", str(files.stack_env),
            "--file", str(root / "compose.langfuse.yaml"), "up", "-d"]


def main(argv=None, *, root=ROOT) -> int:
    parser = argparse.ArgumentParser(description="生成私有 Langfuse 自托管配置；默认不启动 Docker、不覆盖文件。")
    parser.add_argument("--headless", action="store_true", help="初始化组织、项目、管理员与匹配的应用密钥")
    parser.add_argument("--admin-email", help="headless 管理员邮箱；默认 admin@lesson-assistant.local")
    parser.add_argument("--dry-run", action="store_true", help="校验并说明将执行的操作，不创建文件或启动 Docker")
    parser.add_argument("--start", action="store_true", help="配置生成后显式启动独立 Langfuse Compose 项目")
    options = parser.parse_args(argv)
    root = Path(root).resolve()
    try:
        files = prepare_configuration(root, headless=options.headless,
            admin_email=options.admin_email, dry_run=options.dry_run)
        if options.dry_run:
            print(f"dry-run: 将创建 {files.stack_env} 和 {files.app_env}；不输出任何密钥。")
            print(f"dry-run: {'将启动' if options.start else '不会启动'}独立 Docker 项目 {PROJECT}，UI {BASE_URL}。")
            return 0
        print(f"私有配置已创建：{files.stack_env}、{files.app_env}。密钥与管理员密码仅保存在文件中。")
        if not options.start:
            print(f"尚未启动 Docker。请先审查 compose.langfuse.yaml；独立项目名称：{PROJECT}。")
            return 0
        # Never echo Docker's interpolated configuration or raw diagnostics:
        # they can contain credentials. The explicit command touches one project.
        result = subprocess.run(compose_command(root, files), cwd=root, capture_output=True,
                                text=True, check=False, timeout=600)
        if result.returncode:
            raise SetupError("Langfuse Docker 启动失败；已保留配置。请单独检查该 Compose 项目的状态。")
        print(f"已请求启动独立 Langfuse 项目；请检查服务健康状态，UI {BASE_URL}。")
        return 0
    except SetupError as failure:
        print(f"配置未完成：{failure}")
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Langfuse Docker 命令无法完成；已保留配置，请检查 Docker 是否可用。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
