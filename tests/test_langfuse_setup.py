import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import setup_langfuse


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#"))


def test_default_setup_generates_isolated_strong_secrets_and_disables_app_export(tmp_path):
    result = setup_langfuse.prepare_configuration(tmp_path)
    stack, app = read_env(result.stack_env), read_env(result.app_env)
    credentials = [stack[key] for key in (
        "LANGFUSE_PG_PASSWORD", "LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_REDIS_AUTH",
        "LANGFUSE_MINIO_PASSWORD", "LANGFUSE_SALT", "LANGFUSE_NEXTAUTH_SECRET",
    )]
    assert len(set(credentials)) == len(credentials)
    assert all(len(value) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in credentials)
    assert re.fullmatch(r"[0-9a-f]{64}", stack["LANGFUSE_ENCRYPTION_KEY"])
    assert stack["LANGFUSE_AUTH_DISABLE_SIGNUP"] == "false"
    assert not any(key.startswith("LANGFUSE_INIT_") for key in stack)
    assert app["LANGFUSE_ENABLED"] == "false"
    assert app["LANGFUSE_BASE_URL"] == "http://127.0.0.1:3100"
    assert app["LANGFUSE_SECRET_KEY"] == ""
    assert not any("LANGSMITH" in key or key.startswith("AWS_") for key in stack | app)


def test_headless_keys_match_app_config_and_password_is_generated(tmp_path):
    result = setup_langfuse.prepare_configuration(tmp_path, headless=True, admin_email="owner@example.test")
    stack, app = read_env(result.stack_env), read_env(result.app_env)
    assert stack["LANGFUSE_INIT_USER_EMAIL"] == "owner@example.test"
    assert len(stack["LANGFUSE_INIT_USER_PASSWORD"]) >= 43
    assert app["LANGFUSE_PUBLIC_KEY"] == stack["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    assert app["LANGFUSE_SECRET_KEY"] == stack["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
    assert app["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-")
    assert app["LANGFUSE_SECRET_KEY"].startswith("sk-lf-")
    assert app["LANGFUSE_PROJECT"] == stack["LANGFUSE_INIT_PROJECT_NAME"]
    assert app["LANGFUSE_ENABLED"] == "true"
    assert stack["LANGFUSE_AUTH_DISABLE_SIGNUP"] == "true"
    assert app["OBS_CAPTURE_MODE"] == "metadata"


@pytest.mark.parametrize("existing", ["langfuse.env", "langfuse-app.env"])
def test_setup_never_overwrites_either_existing_file(tmp_path, existing):
    cache = tmp_path / ".cache"
    cache.mkdir()
    target = cache / existing
    target.write_text("PRESERVE_USER_CONFIGURATION", encoding="utf-8")
    with pytest.raises(setup_langfuse.SetupError, match="已存在"):
        setup_langfuse.prepare_configuration(tmp_path)
    assert target.read_text(encoding="utf-8") == "PRESERVE_USER_CONFIGURATION"
    assert list(cache.iterdir()) == [target]


@pytest.mark.parametrize("email", ["x@example.test\nEVIL=yes", "$(whoami)@example.test", "bad", "owner@example.test#x"])
def test_invalid_headless_email_cannot_inject_dotenv(tmp_path, email):
    with pytest.raises(setup_langfuse.SetupError, match="邮箱"):
        setup_langfuse.prepare_configuration(tmp_path, headless=True, admin_email=email)
    assert not (tmp_path / ".cache").exists()


def test_admin_email_requires_explicit_headless_mode(tmp_path):
    with pytest.raises(setup_langfuse.SetupError, match="headless"):
        setup_langfuse.prepare_configuration(tmp_path, admin_email="owner@example.test")
    assert not (tmp_path / ".cache").exists()


def test_dry_run_creates_no_files_and_never_starts_docker(tmp_path, capsys, monkeypatch):
    docker = Mock()
    monkeypatch.setattr(setup_langfuse.subprocess, "run", docker)
    assert setup_langfuse.main(["--headless", "--dry-run", "--start"], root=tmp_path) == 0
    assert not (tmp_path / ".cache").exists()
    assert "dry-run" in capsys.readouterr().out
    docker.assert_not_called()


def test_cli_output_does_not_print_credentials_and_start_requires_opt_in(tmp_path, capsys, monkeypatch):
    docker = Mock()
    monkeypatch.setattr(setup_langfuse.subprocess, "run", docker)
    assert setup_langfuse.main(["--headless"], root=tmp_path) == 0
    output = capsys.readouterr().out
    stack = read_env(tmp_path / ".cache/langfuse.env")
    assert stack["LANGFUSE_INIT_USER_PASSWORD"] not in output
    assert stack["LANGFUSE_INIT_PROJECT_SECRET_KEY"] not in output
    assert stack["LANGFUSE_PG_PASSWORD"] not in output
    docker.assert_not_called()


def test_explicit_start_uses_only_the_isolated_compose_without_a_shell(tmp_path, monkeypatch):
    docker = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(setup_langfuse.subprocess, "run", docker)
    assert setup_langfuse.main(["--headless", "--start"], root=tmp_path) == 0
    args, kwargs = docker.call_args
    command = args[0]
    assert command[:2] == ["docker", "compose"]
    assert command[-2:] == ["up", "-d"]
    assert command[command.index("--project-name") + 1] == "lesson-assistant-observability"
    assert command[command.index("--file") + 1] == str(tmp_path / "compose.langfuse.yaml")
    assert command[command.index("--env-file") + 1] == str(tmp_path / ".cache/langfuse.env")
    assert not kwargs.get("shell")
    assert kwargs["capture_output"] is True


def test_docker_failure_keeps_configuration_and_does_not_print_subprocess_secrets(tmp_path, monkeypatch, capsys):
    docker = Mock(return_value=SimpleNamespace(returncode=1, stderr="PRIVATE_DOCKER_CONFIG"))
    monkeypatch.setattr(setup_langfuse.subprocess, "run", docker)
    assert setup_langfuse.main(["--headless", "--start"], root=tmp_path) == 1
    assert (tmp_path / ".cache/langfuse.env").is_file()
    assert "PRIVATE_DOCKER_CONFIG" not in capsys.readouterr().out


def test_cache_symlink_cannot_redirect_credential_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    try:
        (root / ".cache").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a symlink requires Windows developer mode")
    with pytest.raises(setup_langfuse.SetupError, match="路径"):
        setup_langfuse.prepare_configuration(root)
    assert list(outside.iterdir()) == []


def test_failed_second_file_write_rolls_back_only_newly_created_file(tmp_path, monkeypatch):
    original = setup_langfuse.os.open

    def fail_app(path, *args, **kwargs):
        if Path(path).name == "langfuse-app.env":
            raise PermissionError("PRIVATE_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(setup_langfuse.os, "open", fail_app)
    with pytest.raises(setup_langfuse.SetupError, match="写入"):
        setup_langfuse.prepare_configuration(tmp_path)
    assert not (tmp_path / ".cache/langfuse.env").exists()
    assert not (tmp_path / ".cache/langfuse-app.env").exists()
