"""tests/unit/test_env_write_sites.py

Reads and writes of the project `.env` that python-dotenv does not cover (source wizard, source
removal, env helpers, OAuth credential saving, the web secrets API) report an unreadable file
with the one friendly message and never rewrite it; override semantics of the load sites hold.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from dango.config.helpers import save_config
from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CP1252_ENV = b"caf\xe9=1\n"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RAW = ("codec", "0xe9", "invalid continuation", "Traceback", "UnicodeDecodeError")


def _assert_friendly(text: str) -> None:
    flat = " ".join(_ANSI.sub("", text).split())
    assert "Cannot read" in flat and ".env" in flat and "UTF-8" in flat, text
    for raw in RAW:
        assert raw not in text, text


@pytest.fixture
def project(tmp_path: Path, sample_config) -> Path:
    save_config(sample_config, tmp_path)
    return tmp_path


# --- load sites keep their override flag (review L1) ----------------------------------------


def test_google_provider_lets_the_env_file_win_over_a_stale_shell_value(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    (project / ".env").write_bytes(
        b"GOOGLE_CLIENT_ID=from-file\nGOOGLE_CLIENT_SECRET=secret-from-file\n"
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "stale")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "stale-secret")
    monkeypatch.setattr("dango.oauth.providers.Confirm.ask", lambda *a, **k: False)
    seen: list[str] = []
    provider = GoogleOAuthProvider(OAuthManager(project))
    monkeypatch.setattr(
        provider.oauth_manager, "start_oauth_flow", lambda name, url: seen.append(url)
    )
    assert provider.authenticate("google_sheets") is None
    assert parse_qs(urlparse(seen[0]).query)["client_id"] == ["from-file"]


def test_wizard_database_check_lets_the_env_file_win_over_a_stale_shell_value(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.source_wizard import SourceWizard

    (project / ".env").write_bytes(b"WP37_PG_URL=from-file\n")
    monkeypatch.setenv("WP37_PG_URL", "stale")
    wizard = SourceWizard(project)
    wizard.secret_params = [{"param_name": "credentials_env", "name": "WP37_PG_URL"}]
    seen: list[str] = []
    monkeypatch.setattr(wizard, "_validate_database_connection", lambda v: seen.append(v) or True)
    wizard._check_postgres_connections()
    assert seen == ["from-file"]


# --- env helpers ------------------------------------------------------------------------------


def test_create_env_template_reports_an_unreadable_file_and_changes_nothing(
    tmp_path: Path,
) -> None:
    from dango.cli.env_helpers import create_env_template
    from dango.utils.project_env import EnvFileError

    env = tmp_path / ".env"
    env.write_bytes(CP1252_ENV)
    with pytest.raises(EnvFileError) as info:
        create_env_template(env, [{"name": "WP37_A"}])
    _assert_friendly(str(info.value))
    assert env.read_bytes() == CP1252_ENV
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"], "no backup, no temp file"


def test_validate_env_file_reports_an_unreadable_file(tmp_path: Path) -> None:
    from dango.cli.env_helpers import validate_env_file
    from dango.utils.project_env import EnvFileError

    env = tmp_path / ".env"
    env.write_bytes(CP1252_ENV)
    with pytest.raises(EnvFileError):
        validate_env_file(env, ["WP37_A"])


# --- source wizard / source remove ------------------------------------------------------------


def test_wizard_stops_before_asking_for_anything_when_env_is_unreadable(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.source_wizard import SourceWizard

    env = project / ".env"
    env.write_bytes(CP1252_ENV)
    wizard = SourceWizard(project)

    def asked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the wizard asked a question before checking .env")

    monkeypatch.setattr(wizard, "_select_source_flat", asked)
    assert wizard.run() is False
    captured = capsys.readouterr()
    _assert_friendly(captured.out + captured.err)
    assert env.read_bytes() == CP1252_ENV


def test_wizard_secret_prompt_reports_an_unreadable_file(project: Path) -> None:
    from dango.cli.source_wizard import SourceWizard
    from dango.utils.project_env import EnvFileError

    (project / ".env").write_bytes(CP1252_ENV)
    wizard = SourceWizard(project)
    with pytest.raises(EnvFileError):
        wizard._prompt_parameter(
            {"name": "stripe_secret_key_env", "type": "secret"}, "stripe_test", "Stripe", {}
        )


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture
def cli_project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    return working_copy(fresh_project, tmp_path)


def _dango(root: Path, *args: str, stdin: str = "") -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "COLUMNS": "400", "NO_COLOR": "1"}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", *args],
        cwd=root,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_source_add_prints_the_friendly_message_and_leaves_env_alone(cli_project: Path) -> None:
    (cli_project / ".env").write_bytes(CP1252_ENV)
    code, output = _dango(cli_project, "source", "add")
    assert code == 1, output
    _assert_friendly(output)
    assert (cli_project / ".env").read_bytes() == CP1252_ENV
    assert not (cli_project / ".env.backup").exists()


def test_source_remove_reports_the_env_file_and_removes_nothing(cli_project: Path) -> None:
    (cli_project / ".env").write_bytes(CP1252_ENV)
    sources = (cli_project / ".dango" / "sources.yml").read_bytes()
    code, output = _dango(cli_project, "source", "remove", "demo", "--yes")
    assert code == 1, output
    _assert_friendly(output)
    assert (cli_project / ".dango" / "sources.yml").read_bytes() == sources
    assert (cli_project / ".env").read_bytes() == CP1252_ENV


def test_oauth_setup_saves_credentials_the_way_set_key_does(cli_project: Path) -> None:
    (cli_project / ".env").write_bytes(b"WP37_KEEP=1\n")
    code, output = _dango(cli_project, "oauth", "setup", "google", stdin="\nmyid\nmysecret\n")
    assert code == 0, output
    assert (cli_project / ".env").read_bytes() == (
        b"WP37_KEEP=1\nGOOGLE_CLIENT_ID='myid'\nGOOGLE_CLIENT_SECRET='mysecret'\n"
    )


# --- web secrets API --------------------------------------------------------------------------


@pytest.fixture
def web_client(tmp_path: Path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from dango.auth.models import Role, User
    from dango.exceptions import DangoError
    from dango.web.app import dango_error_handler
    from dango.web.routes.secrets import router

    admin = User(
        id="u-admin-1",
        email="admin@test.com",
        password_hash="hashed",
        role=Role.ADMIN,
        is_active=True,
    )
    app = FastAPI()
    app.state.project_root = tmp_path
    app.add_exception_handler(DangoError, dango_error_handler)
    app.include_router(router)

    @app.middleware("http")
    async def set_user(request: Request, call_next: Any) -> Any:
        request.state.user = admin
        request.state.auth_method = "session"
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False), tmp_path


HEADERS = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/secrets", None),
        ("POST", "/api/secrets", {"key": "WP37_A", "value": "x"}),
        ("DELETE", "/api/secrets/WP37_A", None),
    ],
)
def test_secrets_api_answers_422_with_a_fixed_message_for_an_unreadable_env(
    web_client, method: str, path: str, body: dict | None
) -> None:
    client, root = web_client
    (root / ".env").write_bytes(CP1252_ENV)
    response = client.request(method, path, json=body, headers=HEADERS)
    assert response.status_code == 422, response.text
    data = response.json()
    assert set(data) == {"error_code", "message"}
    assert data["message"].startswith("The project .env file cannot be read")
    assert "UTF-8" in data["message"]
    assert str(root) not in response.text, "no server path in the response"
    for raw in RAW:
        assert raw not in response.text
    assert (root / ".env").read_bytes() == CP1252_ENV


def test_secrets_api_still_works_for_a_valid_env(web_client) -> None:
    client, root = web_client
    (root / ".env").write_bytes("WP37_A=cafévalue\n".encode())
    response = client.get("/api/secrets", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["env_vars"][0]["key"] == "WP37_A"
