"""tests/unit/test_caddyfile_ws_auth.py

Marimo WebSocket auth: Caddy forward_auth route, its check endpoint, and the upgrade migration.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import AuthenticationError, AuthorizationError, DangoError
from dango.platform.cloud.ssh import CommandResult


@pytest.fixture(autouse=True)
def _trusted_server_venv() -> Any:
    """SSH mocks here are loose; the venv trust probe is tested in test_runtime_ownership*."""
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


_CADDYFILE = "/etc/caddy/Caddyfile"
_CADDY_TMP = "/etc/caddy/Caddyfile.dango-new"
_HEADER_STRIPS = (
    "header_up -Connection",
    "header_up -Upgrade",
    "header_up -Sec-WebSocket-Key",
    "header_up -Sec-WebSocket-Version",
    "header_up -Sec-WebSocket-Extensions",
)

# Exactly what build_caddyfile() rendered in 1.0.13 (the pre-fix template).
_V1013_DOMAIN = (
    "a.example {\n\thandle /notebooks/marimo/ws {\n\t\treverse_proxy localhost:7805\n\t}\n"
    "\thandle {\n\t\treverse_proxy localhost:8800\n\t}\n\theader {\n"
    "\t\tX-Content-Type-Options nosniff\n\t\tX-Frame-Options SAMEORIGIN\n"
    "\t\tReferrer-Policy strict-origin-when-cross-origin\n"
    '\t\tPermissions-Policy "interest-cohort=()"\n'
    '\t\tStrict-Transport-Security "max-age=63072000; includeSubDomains"\n\t}\n}\n\n'
    ':80 {\n\trespond "Use the configured domain to access this server." 403\n}\n'
)
_V1013_HTTP = (
    ":80 {\n\thandle /notebooks/marimo/ws {\n\t\treverse_proxy localhost:7805\n\t}\n"
    "\thandle {\n\t\treverse_proxy localhost:8800\n\t}\n\theader {\n"
    "\t\tX-Content-Type-Options nosniff\n\t\tX-Frame-Options SAMEORIGIN\n"
    "\t\tReferrer-Policy strict-origin-when-cross-origin\n"
    '\t\tPermissions-Policy "interest-cohort=()"\n\t}\n}\n'
)
_OLD_BLOCK_7805 = "\thandle /notebooks/marimo/ws {\n\t\treverse_proxy localhost:7805\n\t}"
_HAND_EDITED = (
    "{\n\temail ops@example.com\n}\n\n"
    "a.com, b.com {\n\tencode gzip\n"
    "\thandle /notebooks/marimo/ws {\n\t\treverse_proxy localhost:7811\n\t}\n"
    "\thandle {\n\t\treverse_proxy localhost:8800\n\t}\n}\n\n"
    'extra.example {\n\trespond "hello" 200\n}\n'
)


def _new_block(port: int) -> str:
    """The authenticated WebSocket handle the 1.0.14 template must render."""
    return (
        "\thandle /notebooks/marimo/ws {\n"
        "\t\tforward_auth localhost:8800 {\n"
        "\t\t\turi /api/notebooks/ws-auth\n"
        "\t\t\theader_up -Connection\n"
        "\t\t\theader_up -Upgrade\n"
        "\t\t\theader_up -Sec-WebSocket-Key\n"
        "\t\t\theader_up -Sec-WebSocket-Version\n"
        "\t\t\theader_up -Sec-WebSocket-Extensions\n"
        "\t\t}\n"
        f"\t\treverse_proxy localhost:{port}\n"
        "\t}"
    )


def _ws_handles(caddyfile: str) -> list[str]:
    """Return the body of every ``handle /notebooks/marimo/ws`` block."""
    bodies = []
    for m in re.finditer(r"handle /notebooks/marimo/ws \{\n", caddyfile):
        end = caddyfile.index("\n\t}", m.end())
        bodies.append(caddyfile[m.end() : end])
    return bodies


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("domain", ["a.example", None], ids=["https", "http"])
def test_caddyfile_ws_route_requires_forward_auth(domain: str | None) -> None:
    """Every Marimo WebSocket route is behind forward_auth to the permission check."""
    from dango.platform.cloud._server_templates import build_caddyfile

    content = build_caddyfile(domain=domain, marimo_port=7811)
    bodies = _ws_handles(content)
    assert len(bodies) == 1
    body = bodies[0]
    assert "forward_auth localhost:8800 {" in body
    assert "uri /api/notebooks/ws-auth" in body
    for line in _HEADER_STRIPS:
        assert line in body
    assert body.index("forward_auth") < body.index("reverse_proxy localhost:7811")
    assert _new_block(7811) in content


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _client(role: Role | None) -> TestClient:
    """Notebooks router with a user of *role* injected (``None`` = anonymous)."""
    from dango.web.routes.notebooks import router

    app = FastAPI()

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        code = 401 if isinstance(exc, AuthenticationError) else 500
        if isinstance(exc, AuthorizationError):
            code = 403
        return JSONResponse(status_code=code, content={"error_code": exc.error_code})

    if role is not None:
        user = User(id="u-1", email="u@example.com", password_hash="x", role=role, is_active=True)

        @app.middleware("http")
        async def _set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            request.state.auth_method = "session"
            return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "expected"),
    [(None, 401), (Role.VIEWER, 403), (Role.EDITOR, 200), (Role.ADMIN, 200)],
    ids=["anonymous", "viewer", "editor", "admin"],
)
def test_ws_auth_endpoint_permission_matrix(role: Role | None, expected: int) -> None:
    """forward_auth target answers 200 only to sessions that may execute notebooks."""
    resp = _client(role).get("/api/notebooks/ws-auth")
    assert resp.status_code == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "expected"), [(None, 401), (Role.VIEWER, 200)], ids=["anonymous", "viewer"]
)
def test_notebook_status_requires_session(tmp_path: Path, role: Role | None, expected: int) -> None:
    """Notebook status polling needs a session with ``notebooks.view``."""
    with patch("dango.web.routes.notebooks.get_project_root", return_value=tmp_path):
        resp = _client(role).get("/api/notebooks/status")
    assert resp.status_code == expected


# ---------------------------------------------------------------------------
# Upgrade migration
# ---------------------------------------------------------------------------


class _FakeServer:
    """Stateful SSH stand-in: a tiny file system plus scripted command results."""

    def __init__(
        self,
        caddyfile: str | None,
        *,
        validate_ok: bool = True,
        reload_results: list[bool] | None = None,
        installed: str | None = "1.0.14",
    ) -> None:
        self.files: dict[str, str] = {} if caddyfile is None else {_CADDYFILE: caddyfile}
        # Version reported by the server venv: before pip install, then after it.
        self.versions: list[str | None] = ["1.0.13", installed]
        self.validate_ok = validate_ok
        self.reload_results = list(reload_results or [])
        self.log: list[str] = []
        self.caddyfile_writes: list[str] = []

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.log.append(command)
        ok = CommandResult(stdout="", stderr="", exit_code=0)
        fail = CommandResult(stdout="", stderr="boom", exit_code=1)
        if "import dango" in command:
            version = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
            if version is None:
                return fail
            return CommandResult(stdout=version, stderr="", exit_code=0)
        if command.startswith(f"cat {_CADDYFILE}") and _CADDY_TMP not in command:
            if _CADDYFILE not in self.files:
                return fail
            return CommandResult(stdout=self.files[_CADDYFILE], stderr="", exit_code=0)
        if command.startswith("caddy validate"):
            assert _CADDY_TMP in command
            assert _CADDY_TMP in self.files, "validated a temp file that was never written"
            return ok if self.validate_ok else fail
        if "reload-or-restart caddy" in command:
            return ok if (self.reload_results.pop(0) if self.reload_results else True) else fail
        if command.startswith("rm ") and _CADDY_TMP in command:
            self.files.pop(_CADDY_TMP, None)
        return ok

    def write_remote_file(
        self, remote_path: str, content: str | bytes, mode: int = 0o644, timeout: int | None = None
    ) -> None:
        text = content.decode() if isinstance(content, bytes) else content
        self.log.append(f"WRITE {remote_path}")
        if remote_path == _CADDYFILE:
            self.caddyfile_writes.append(text)
        self.files[remote_path] = text


def _upgrade(server: _FakeServer, version: str = "1.0.14", *, force: bool = False) -> Any:
    from dango.platform.cloud.upgrade import upgrade_dango

    with patch("dango.platform.cloud.backup.time.sleep"):
        return upgrade_dango(server, version=version, skip_backup=True, force=force)


def _assert_upgrade_completed(server: _FakeServer, result: Any) -> None:
    assert result.health_check_passed is True
    assert "systemctl start dango-web" in server.log
    assert _CADDY_TMP not in server.files


@pytest.mark.unit
def test_upgrade_migrates_ws_block_domain_file() -> None:
    """1.0.13 HTTPS file: only the ws block changes, written once, before services restart."""
    server = _FakeServer(_V1013_DOMAIN)
    result = _upgrade(server)

    expected = _V1013_DOMAIN.replace(_OLD_BLOCK_7805, _new_block(7805))
    assert server.caddyfile_writes == [expected]
    assert expected.startswith("a.example {\n")
    assert expected.endswith(_V1013_DOMAIN[_V1013_DOMAIN.index("\theader {") :])
    assert result.caddyfile_ws_migration == "Caddyfile WebSocket route migrated"
    assert server.log.index(f"WRITE {_CADDYFILE}") < server.log.index("systemctl start dango-web")
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migrates_ws_block_http_file() -> None:
    """1.0.13 HTTP-only file: only the ws block changes."""
    server = _FakeServer(_V1013_HTTP)
    result = _upgrade(server)

    assert server.caddyfile_writes == [_V1013_HTTP.replace(_OLD_BLOCK_7805, _new_block(7805))]
    assert result.caddyfile_ws_migration == "Caddyfile WebSocket route migrated"
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migration_preserves_hand_edits() -> None:
    """Hand-edited file: the custom port is kept and nothing outside the block changes."""
    server = _FakeServer(_HAND_EDITED)
    result = _upgrade(server)

    old_block = "\thandle /notebooks/marimo/ws {\n\t\treverse_proxy localhost:7811\n\t}"
    assert server.caddyfile_writes == [_HAND_EDITED.replace(old_block, _new_block(7811))]
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migration_idempotent() -> None:
    """Already-migrated file: no write, reported as already migrated."""
    migrated = _V1013_DOMAIN.replace(_OLD_BLOCK_7805, _new_block(7805))
    server = _FakeServer(migrated)
    result = _upgrade(server)

    assert server.caddyfile_writes == []
    assert server.files[_CADDYFILE] == migrated
    assert result.caddyfile_ws_migration == "Caddyfile WebSocket route already migrated"
    assert not any("Caddyfile" in w for w in result.warnings)
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
@pytest.mark.parametrize(
    "caddyfile",
    [
        ":80 {\n\thandle /notebooks/marimo/ws {\n\t\treverse_proxy 127.0.0.1:7805\n\t}\n}\n",
        _V1013_HTTP + "\n:8080 {\n" + _OLD_BLOCK_7805 + "\n}\n",
        None,
    ],
    ids=["block-not-matching", "block-twice", "unreadable"],
)
def test_upgrade_migration_skips_unexpected_file(caddyfile: str | None) -> None:
    """Unexpected Caddyfile: no write, a warning naming the file, upgrade continues."""
    server = _FakeServer(caddyfile)
    result = _upgrade(server)

    assert server.caddyfile_writes == []
    assert result.caddyfile_ws_migration.startswith("Caddyfile WebSocket route skipped: ")
    assert any(_CADDYFILE in w for w in result.warnings), result.warnings
    if caddyfile is not None:
        assert any("forward_auth" in w for w in result.warnings), result.warnings
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migration_validate_failure_no_write() -> None:
    """``caddy validate`` rejects the candidate: no write, temp file removed, warning."""
    server = _FakeServer(_V1013_DOMAIN, validate_ok=False)
    result = _upgrade(server)

    assert any(c.startswith("caddy validate") for c in server.log)
    assert server.caddyfile_writes == []
    assert server.files[_CADDYFILE] == _V1013_DOMAIN
    assert result.caddyfile_ws_migration.startswith("Caddyfile WebSocket route skipped: ")
    assert any(_CADDYFILE in w for w in result.warnings), result.warnings
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migration_reload_failure_restores_original() -> None:
    """New file written but Caddy reload fails: original restored and reloaded, warning."""
    server = _FakeServer(_V1013_DOMAIN, reload_results=[False, True])
    result = _upgrade(server)

    assert server.caddyfile_writes[-1] == _V1013_DOMAIN
    assert server.files[_CADDYFILE] == _V1013_DOMAIN
    assert sum("reload-or-restart caddy" in c for c in server.log) == 2
    assert result.caddyfile_ws_migration.startswith("Caddyfile WebSocket route skipped: ")
    assert any("restored" in w for w in result.warnings), result.warnings
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
def test_upgrade_migration_no_ws_route_needs_nothing() -> None:
    """A Caddyfile without any Marimo WebSocket handle needs no migration and no warning."""
    caddyfile = ":80 {\n\thandle {\n\t\treverse_proxy localhost:8800\n\t}\n}\n"
    server = _FakeServer(caddyfile)
    result = _upgrade(server)

    assert server.caddyfile_writes == []
    assert result.caddyfile_ws_migration == (
        "Caddyfile WebSocket route: no migration needed (no direct WebSocket route)"
    )
    assert not any(_CADDYFILE in w for w in result.warnings), result.warnings
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("installed", "requested", "force"),
    [("1.0.13", "1.0.13", False), ("1.0.12", "1.0.12", True), (None, "1.0.14", False)],
    ids=["installed-1.0.13", "downgrade-1.0.12", "installed-unreadable"],
)
def test_upgrade_migration_requires_installed_ws_auth(
    installed: str | None, requested: str, force: bool
) -> None:
    """Only a server that now runs a version with /api/notebooks/ws-auth is migrated."""
    server = _FakeServer(_V1013_DOMAIN, installed=installed)
    server.versions[0] = "1.0.12" if requested == "1.0.13" else "1.0.13"
    result = _upgrade(server, requested, force=force)

    assert server.caddyfile_writes == []
    assert server.files[_CADDYFILE] == _V1013_DOMAIN
    assert not any(c.startswith("caddy validate") for c in server.log)
    assert result.caddyfile_ws_migration.startswith("Caddyfile WebSocket route skipped: ")
    assert any(_CADDYFILE in w for w in result.warnings), result.warnings
    _assert_upgrade_completed(server, result)


@pytest.mark.unit
@pytest.mark.parametrize("installed", ["1.0.14", "1.0.15", "1.1.0rc1"])
def test_upgrade_migration_runs_for_installed_ws_auth(installed: str) -> None:
    """A server now on 1.0.14 or later is migrated."""
    server = _FakeServer(_V1013_HTTP, installed=installed)
    result = _upgrade(server, installed)

    assert server.caddyfile_writes == [_V1013_HTTP.replace(_OLD_BLOCK_7805, _new_block(7805))]
    assert result.caddyfile_ws_migration == "Caddyfile WebSocket route migrated"


@pytest.mark.unit
def test_upgrade_at_target_version_still_migrates_legacy_block() -> None:
    """Re-running upgrade at the installed version (>= 1.0.14) migrates a leftover old block."""
    server = _FakeServer(_V1013_HTTP)
    server.versions = ["1.0.14", "1.0.14"]
    result = _upgrade(server, "1.0.14")

    assert server.caddyfile_writes == [_V1013_HTTP.replace(_OLD_BLOCK_7805, _new_block(7805))]
    assert result.caddyfile_ws_migration == "Caddyfile WebSocket route migrated"
    assert any("Already at target version" in w for w in result.warnings)
    assert _CADDY_TMP not in server.files


@pytest.mark.unit
def test_upgrade_unreadable_version_warning_gives_manual_step() -> None:
    """When the installed version cannot be read, the warning names the manual step."""
    server = _FakeServer(_V1013_DOMAIN, installed=None)
    result = _upgrade(server)

    warning = next(w for w in result.warnings if _CADDYFILE in w)
    assert "forward_auth" in warning
