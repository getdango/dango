"""tests/unit/test_env_rewrite_roundtrip.py

Rewriting the project .env (Secrets page, source removal, remote env) keeps every other value, comment and line as it was.
"""

from __future__ import annotations

import ast
import json
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
from dango.utils.env_file import parse_env_file
from dango.utils.project_env import parse_env_text, read_env, set_env_value
from dango.web.routes.secrets import router

pytestmark = pytest.mark.unit

HEADERS = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Obviously fake values: every character class a rewrite has to carry through unchanged.
VALUES: dict[str, str] = {
    "JSON_CRED": json.dumps({"type": "service_account", "client_email": "fake@example.invalid"}),
    "WIN_PATH": "C:\\Program Files\\Fake App\\key.json",
    "UNC_PATH": "\\\\fake-server\\share\\file",
    "DQUOTE": 'fake"pass"word',
    "BACKSLASH_END": "fake\\",
    "DOLLAR": "fake$pass",
    "HASH": "fake # not a comment",
    "EQUALS": "a=b=c",
    "TRAILING_SPACE": "fake  ",
    "LEADING_SPACE": "  fake",
    "MULTILINE": "line1\nline2",
    "SQUOTE": "it's fake",
    "BACKSLASH_N": "fake\\nliteral",
}
HEAD = "# my notes\n\nPLAIN=abc\n"


def _admin() -> User:
    return User(
        id="u-admin-1",
        email="admin@test.invalid",
        password_hash="hashed",
        role=Role.ADMIN,
        is_active=True,
    )


def _client(root: Path) -> TestClient:
    """The real secrets router with an admin injected (same shape as test_web_secrets)."""
    app = FastAPI()
    app.state.project_root = root

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"message": exc.user_message})

    @app.middleware("http")
    async def _user(request: Any, call_next: Any) -> Any:
        request.state.user = _admin()
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _seed(root: Path, name: str, value: str) -> None:
    """A .env with a comment, a blank line, one plain line and NAME=value (dotenv-quoted)."""
    (root / ".env").write_text(HEAD, encoding="utf-8")
    set_env_value(root / ".env", name, value)


@pytest.mark.parametrize("name", sorted(VALUES))
def test_secrets_page_saves_keep_the_other_values_and_lines(tmp_path: Path, name: str) -> None:
    _seed(tmp_path, name, VALUES[name])
    client = _client(tmp_path)

    assert (
        client.post(
            "/api/secrets", json={"key": "NEW_ONE", "value": "x1"}, headers=HEADERS
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/secrets", json={"key": "NEW_TWO", "value": "x2"}, headers=HEADERS
        ).status_code
        == 200
    )
    assert client.delete("/api/secrets/NEW_ONE", headers=HEADERS).status_code == 200

    values = read_env(tmp_path)
    assert values[name] == VALUES[name]
    assert values["PLAIN"] == "abc"
    assert values["NEW_TWO"] == "x2" and "NEW_ONE" not in values
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert text.startswith(HEAD), "comment, blank line and plain line stay byte-for-byte"


def test_secrets_page_stores_a_posted_special_value_exactly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for name, value in VALUES.items():
        resp = client.post("/api/secrets", json={"key": name, "value": value}, headers=HEADERS)
        assert resp.status_code == 200
    client.post("/api/secrets", json={"key": "LAST", "value": "z"}, headers=HEADERS)
    values = read_env(tmp_path)
    for name, value in VALUES.items():
        assert values[name] == value, name


def test_secrets_page_controls_unchanged(tmp_path: Path) -> None:
    """Ordinary values, masking, 404 on a missing key, audit events (green before and after)."""
    (tmp_path / ".env").write_text("FOO=bar\nBAZ=quux12345\n", encoding="utf-8")
    client = _client(tmp_path)
    with patch("dango.web.routes.secrets.log_auth_event") as audit:
        assert (
            client.post("/api/secrets", json={"key": "BAZ", "value": "newvalue99"}).status_code
            == 200
        )
        assert client.delete("/api/secrets/FOO", headers=HEADERS).status_code == 200
        assert client.delete("/api/secrets/NOPE", headers=HEADERS).status_code == 404
    assert [c.args[0].value for c in audit.call_args_list] == ["secret_set", "secret_deleted"]
    listed = client.get("/api/secrets", headers=HEADERS).json()["env_vars"]
    assert listed == [{"key": "BAZ", "masked_value": "****ue99", "source": "env"}]
    assert client.post("/api/secrets", json={"value": "x"}, headers=HEADERS).status_code == 400


def _remove_project(root: Path, sample_config: Any, env_text: str) -> None:
    sample_config.sources.sources = [
        DataSource(
            name="payments",
            type="local_files",
            local_files={"directory": "data/uploads/payments", "file_pattern": "*.csv"},
        )
    ]
    save_config(sample_config, root)
    (root / ".env").write_text(env_text, encoding="utf-8")


@pytest.mark.parametrize("name", ["JSON_CRED", "WIN_PATH", "DQUOTE", "MULTILINE"])
def test_source_remove_then_a_secrets_save_keeps_other_credentials(
    tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from dango.cli.main import cli

    _remove_project(tmp_path, sample_config, HEAD + "PAYMENTS_API_KEY=fake-key-1234\n")
    set_env_value(tmp_path / ".env", name, VALUES[name])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: (True, ""))

    result = CliRunner().invoke(cli, ["source", "remove", "payments", "--yes"], input="y\n")

    assert result.exit_code == 0, result.output
    after_remove = read_env(tmp_path)
    assert "PAYMENTS_API_KEY" not in after_remove
    assert after_remove[name] == VALUES[name]
    assert (tmp_path / ".env").read_text(encoding="utf-8").startswith(HEAD)
    client = _client(tmp_path)
    assert (
        client.post(
            "/api/secrets", json={"key": "OTHER", "value": "x"}, headers=HEADERS
        ).status_code
        == 200
    )
    assert read_env(tmp_path)[name] == VALUES[name]


def test_parse_env_file_reads_what_the_runtime_reads() -> None:
    text = (
        'ESC="a\\"b\\\\c"\n'
        "SINGLE='it\\'s'\n"
        'MULTI="line1\nline2"\n'
        "# comment\n"
        "no-equals-line\n"
        "PLAIN=abc\n"
    )
    expected = {k: v for k, v in parse_env_text(text).items() if v is not None}
    assert parse_env_file(text) == expected
    assert parse_env_file(text)["MULTI"] == "line1\nline2"
    assert parse_env_file(text)["ESC"] == 'a"b\\c'
    assert "no-equals-line" not in parse_env_file(text)


def test_parse_env_file_does_not_expand_references_from_the_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_LOCAL_VAR", "from-this-machine")
    assert parse_env_file("A=${FAKE_LOCAL_VAR}\n") == {"A": "${FAKE_LOCAL_VAR}"}


# --- remote .env (text in, text out) --------------------------------------------------------


def _ssh(env_text: str) -> MagicMock:
    from dango.platform.cloud.ssh import CommandResult

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=env_text, stderr="", exit_code=0)
    return ssh


def _run_remote(ssh: MagicMock, *args: str) -> str:
    with patch(
        "dango.cli.commands.remote._ssh_connect_or_fail",
        return_value=(MagicMock(), ssh, Path("/test/project")),
    ):
        result = CliRunner().invoke(remote_env, list(args), catch_exceptions=False)
    assert result.exit_code == 0, _ANSI.sub("", result.output)
    return str(ssh.write_remote_file.call_args[0][1])


@pytest.mark.parametrize("name", sorted(VALUES))
def test_remote_env_set_and_delete_keep_the_other_lines(name: str) -> None:
    server = HEAD + f"{name}={_dotenv_line(VALUES[name])}\n"

    after_set = _run_remote(_ssh(server), "set", "NEW_ONE=fresh")
    assert after_set.startswith(server), (
        "existing lines stay byte-for-byte; the new one is appended"
    )
    assert parse_env_text(after_set)[name] == VALUES[name]

    after_delete = _run_remote(_ssh(after_set), "delete", "NEW_ONE")
    assert after_delete == server
    assert parse_env_text(after_delete)[name] == VALUES[name]


def _dotenv_line(value: str) -> str:
    """The right-hand side dotenv itself writes for NAME=value (single-quoted, escaped)."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# --- the reader embedded in server scripts stays free of Dango imports ----------------------


def test_the_spaces_script_reader_has_no_dango_import() -> None:
    from dango.platform.cloud.migrate import _build_spaces_upload_script

    script = _build_spaces_upload_script("/a", "r", "https://e.invalid", "AK", "SK", "b", "k")
    ast.parse(script)
    for forbidden in ("import dango", "from dango", "project_env", "dotenv"):
        assert forbidden not in script


def test_a_secrets_save_tightens_a_loose_env_file_and_never_leaves_temp_files(
    tmp_path: Path,
) -> None:
    import os
    import stat

    env = tmp_path / ".env"
    env.write_text(HEAD, encoding="utf-8")
    os.chmod(env, 0o644)
    client = _client(tmp_path)
    assert client.post("/api/secrets", json={"key": "K", "value": "v1"}).status_code == 200
    expected = 0o600 if os.name == "posix" else stat.S_IMODE(os.stat(env).st_mode)
    assert stat.S_IMODE(os.stat(env).st_mode) == expected
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


@pytest.mark.parametrize("name", [n for n in sorted(VALUES) if VALUES[n] == VALUES[n].strip()])
def test_remote_env_set_writes_a_special_value_the_runtime_reads_back(name: str) -> None:
    written = _run_remote(_ssh(HEAD), "set", f"{name}={VALUES[name]}")
    assert written.startswith(HEAD)
    assert parse_env_text(written)[name] == VALUES[name]
    again = _run_remote(_ssh(written), "set", "OTHER=x1")
    assert parse_env_text(again)[name] == VALUES[name]
