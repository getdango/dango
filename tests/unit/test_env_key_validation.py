"""tests/unit/test_env_key_validation.py

A variable name that cannot be written to a .env on one line is refused by the Secrets page, remote env set and source removal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.cli.commands.remote_env import env as remote_env
from dango.config.helpers import save_config
from dango.config.models import DataSource
from dango.exceptions import DangoError
from dango.utils.project_env import read_env
from dango.web.routes.secrets import router

pytestmark = pytest.mark.unit

HEADERS = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}
SEED = "# notes\nPLAIN=abc\n"
BAD_KEYS = [
    "K\nINJECTED",
    "K\rINJECTED",
    "K2=X",
    "A B",
    "A\tB",
    "A B",
    "A B",
    "#X",
    "A#B",
]
GOOD_KEYS = ["API_KEY", "my.key", "A-B", "a_1", "STRIPE_API_KEY2"]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _client(root: Path) -> TestClient:
    app = FastAPI()
    app.state.project_root = root

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"message": exc.user_message})

    @app.middleware("http")
    async def _user(request: Any, call_next: Any) -> Any:
        request.state.user = User(
            id="u-admin-1",
            email="admin@test.invalid",
            password_hash="hashed",
            role=Role.ADMIN,
            is_active=True,
        )
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("key", BAD_KEYS)
def test_secrets_page_refuses_a_key_that_cannot_be_one_line(tmp_path: Path, key: str) -> None:
    (tmp_path / ".env").write_text(SEED, encoding="utf-8")
    with patch("dango.web.routes.secrets.log_auth_event") as audit:
        resp = _client(tmp_path).post("/api/secrets", json={"key": key, "value": "v"})
    assert resp.status_code == 400
    assert "Key" in resp.json()["message"]
    assert (tmp_path / ".env").read_text(encoding="utf-8") == SEED
    assert "INJECTED" not in read_env(tmp_path) and "X" not in read_env(tmp_path)
    audit.assert_not_called()


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_secrets_page_still_accepts_ordinary_keys(tmp_path: Path, key: str) -> None:
    (tmp_path / ".env").write_text(SEED, encoding="utf-8")
    client = _client(tmp_path)
    with patch("dango.web.routes.secrets.log_auth_event") as audit:
        resp = client.post("/api/secrets", json={"key": key, "value": "value1234"})
    assert resp.status_code == 200
    assert read_env(tmp_path)[key] == "value1234"
    assert audit.call_args.kwargs["details"] == {"key": key, "action": "created"}
    listed = {i["key"]: i["masked_value"] for i in client.get("/api/secrets").json()["env_vars"]}
    assert listed[key] == "****1234"


def _ssh(text: str) -> MagicMock:
    from dango.platform.cloud.ssh import CommandResult

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=text, stderr="", exit_code=0)
    return ssh


def _set(ssh: MagicMock, arg: str) -> Any:
    with patch(
        "dango.cli.commands.remote._ssh_connect_or_fail",
        return_value=(MagicMock(), ssh, Path("/test/project")),
    ):
        return CliRunner().invoke(remote_env, ["set", arg])


@pytest.mark.parametrize("key", [k for k in BAD_KEYS if "=" not in k])
def test_remote_env_set_refuses_such_a_key_and_writes_nothing(key: str) -> None:
    ssh = _ssh(SEED)
    result = _set(ssh, f"{key}=v")
    assert result.exit_code != 0
    assert "key" in _ANSI.sub("", result.output).lower()
    ssh.write_remote_file.assert_not_called()


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_remote_env_set_still_accepts_ordinary_keys(key: str) -> None:
    ssh = _ssh(SEED)
    assert _set(ssh, f"{key}=v").exit_code == 0
    assert ssh.write_remote_file.call_args[0][1].startswith(SEED)


def test_source_remove_leaves_a_variable_whose_name_is_not_a_plain_name(
    tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.main import cli

    sample_config.sources.sources = [
        DataSource(
            name="payments",
            type="local_files",
            local_files={"directory": "data/uploads/payments", "file_pattern": "*.csv"},
        )
    ]
    save_config(sample_config, tmp_path)
    odd = "'PAYMENTS_BAD KEY'=two\n"
    (tmp_path / ".env").write_text(SEED + "PAYMENTS_API_KEY=one\n" + odd, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: (True, ""))

    result = CliRunner().invoke(cli, ["source", "remove", "payments", "--yes"], input="y\n")

    assert result.exit_code == 0, result.output
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PAYMENTS_API_KEY" not in text
    assert text == SEED + odd
