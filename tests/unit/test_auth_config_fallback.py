"""tests/unit/test_auth_config_fallback.py

Tests for fail-safe auth config loading in dango/config/auth_loading.py (1.0.13-T21 / C27).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password, hash_token
from dango.config.auth_loading import load_auth_config_safe
from dango.config.models import AuthConfig
from dango.migrations.runner import MigrationRunner
from dango.platform.cloud.server_auth import CLOUD_AUTH_TIMEOUTS
from dango.web.app import _load_auth_config
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth import router as auth_router
from dango.web.routes.auth_2fa import router as auth_2fa_router

PATCH_LOGGER = "dango.config.auth_loading.logger"


def _write_project(root: Path, auth_yaml: str) -> Path:
    dango_dir = root / ".dango"
    dango_dir.mkdir()
    project_yml = dango_dir / "project.yml"
    project_yml.write_text(
        "project:\n  name: t\n  created: '2026-01-01T00:00:00Z'\n  dango_version: 1.0.0\n"
        "  organization: o\n  purpose: p\n"
        f"{auth_yaml}"
    )
    return project_yml


@pytest.fixture(autouse=True)
def _reset_warned():
    from dango.config import auth_loading

    auth_loading._warned.clear()
    yield
    auth_loading._warned.clear()


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


@pytest.fixture
def cloud_mode(monkeypatch):
    monkeypatch.setenv("DANGO_CLOUD_MODE", "true")


BAD_AUTH = [
    pytest.param("auth:\n  session_max_days:\n", "session_max_days", id="null"),
    pytest.param("auth:\n  session_max_days: forever\n", "session_max_days", id="string"),
    pytest.param("auth:\n  idle_timeout_minutes: soon\n", "idle_timeout_minutes", id="idle"),
]


class TestValid:
    def test_valid_config_unchanged(self, tmp_path, local_mode):
        _write_project(tmp_path, "auth:\n  session_max_days: 7\n  idle_timeout_minutes: 15\n")
        with patch(PATCH_LOGGER) as log:
            cfg = _load_auth_config(tmp_path)
        assert cfg is not None
        assert (cfg.session_max_days, cfg.idle_timeout_minutes) == (7, 15)
        log.warning.assert_not_called()

    def test_valid_config_unchanged_in_cloud_mode(self, tmp_path, cloud_mode):
        _write_project(tmp_path, "auth:\n  session_max_days: 7\n")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and cfg.session_max_days == 7

    def test_no_auth_section_gives_defaults(self, tmp_path, local_mode):
        _write_project(tmp_path, "")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and cfg.session_max_days == 365

    def test_missing_project_returns_none_without_warning(self, tmp_path, local_mode):
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_not_called()


class TestInvalidLocal:
    @pytest.mark.parametrize(("auth_yaml", "field"), BAD_AUTH)
    def test_warns_and_keeps_local_defaults(self, tmp_path, local_mode, auth_yaml, field):
        project_yml = _write_project(tmp_path, auth_yaml)
        with patch(PATCH_LOGGER) as log:
            cfg = load_auth_config_safe(tmp_path)
        assert cfg is None  # callers fall back to AuthConfig() local defaults
        log.warning.assert_called_once()
        event = log.warning.call_args
        assert event.args[0] == "auth_config_invalid"
        assert event.kwargs["file"] == str(project_yml)
        assert field in event.kwargs["field"]
        assert event.kwargs["fallback"] == "local_defaults"

    def test_invalid_yaml_warns(self, tmp_path, local_mode):
        project_yml = _write_project(tmp_path, "auth: [unclosed\n")
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["file"] == str(project_yml)

    def test_unreadable_file_warns(self, tmp_path, local_mode):
        project_yml = _write_project(tmp_path, "")
        project_yml.write_bytes(b"\xff\xfe\x00bad")
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_called_once()


class TestInvalidCloud:
    @pytest.mark.parametrize(("auth_yaml", "field"), BAD_AUTH)
    def test_falls_back_to_cloud_values(self, tmp_path, cloud_mode, auth_yaml, field):
        _write_project(tmp_path, auth_yaml)
        with patch(PATCH_LOGGER) as log:
            cfg = _load_auth_config(tmp_path)
        assert cfg is not None
        assert cfg.session_max_days == 30
        assert cfg.idle_timeout_minutes == 60
        assert cfg.enabled is True
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["fallback"] == "cloud_defaults"

    def test_invalid_yaml_cloud_values(self, tmp_path, cloud_mode):
        _write_project(tmp_path, "auth: [unclosed\n")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and (cfg.session_max_days, cfg.idle_timeout_minutes) == (30, 60)


def test_warning_does_not_echo_secret_values(tmp_path, local_mode):
    _write_project(
        tmp_path,
        "auth:\n  session_max_days: s3cret-value-xyz\n",
    )
    with patch(PATCH_LOGGER) as log:
        load_auth_config_safe(tmp_path)
    assert "s3cret-value-xyz" not in str(log.warning.call_args)


def test_cloud_constants_match_server_auth():
    cfg = AuthConfig(**CLOUD_AUTH_TIMEOUTS)
    assert (cfg.session_max_days, cfg.idle_timeout_minutes) == (30, 60)


class TestWarnOnce:
    def test_repeated_loads_warn_once(self, tmp_path, local_mode):
        _write_project(tmp_path, "auth:\n  session_max_days:\n")
        with patch(PATCH_LOGGER) as log:
            load_auth_config_safe(tmp_path)
            load_auth_config_safe(tmp_path)
            _load_auth_config(tmp_path)
        log.warning.assert_called_once()


class TestMissingProject:
    def test_cloud_missing_project_warns_and_uses_cloud_values(self, tmp_path, cloud_mode):
        with patch(PATCH_LOGGER) as log:
            cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and (cfg.session_max_days, cfg.idle_timeout_minutes) == (30, 60)
        log.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Route helpers: sessions created by login / 2FA honour the fallback
# ---------------------------------------------------------------------------


def _setup_app(tmp_path: Path, auth_yaml: str, **user_kw: Any) -> tuple[TestClient, Path]:
    _write_project(tmp_path, auth_yaml)
    db_path = tmp_path / ".dango" / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir).apply_pending()
    db.create_user(
        db_path,
        User(
            email="u@example.com",
            role=Role.EDITOR,
            password_hash=hash_password("password12345"),
            **user_kw,
        ),
    )
    app = FastAPI()
    app.state.project_root = tmp_path
    app.include_router(auth_router)
    app.include_router(auth_2fa_router)
    return TestClient(app, raise_server_exceptions=False), db_path


_H = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}
_LOGIN = {"email": "u@example.com", "password": "password12345"}


def _session_days(db_path: Path, response: Any) -> float:
    raw = response.headers["set-cookie"].split(f"{COOKIE_NAME}=")[1].split(";")[0]
    session = db.get_session_by_token(db_path, hash_token(raw))
    assert session is not None
    return (session.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400


class TestRouteSessionExpiry:
    def test_login_cloud_invalid_auth_gets_30_days(self, tmp_path, cloud_mode):
        client, db_path = _setup_app(tmp_path, "auth:\n  session_max_days:\n")
        resp = client.post("/api/auth/login", json=_LOGIN, headers=_H)
        assert resp.status_code == 200
        assert 29.9 < _session_days(db_path, resp) < 30.1

    def test_login_local_invalid_auth_keeps_365_days(self, tmp_path, local_mode):
        client, db_path = _setup_app(tmp_path, "auth:\n  session_max_days:\n")
        resp = client.post("/api/auth/login", json=_LOGIN, headers=_H)
        assert resp.status_code == 200
        assert 364.9 < _session_days(db_path, resp) < 365.1

    def test_login_cloud_valid_auth_unchanged(self, tmp_path, cloud_mode):
        client, db_path = _setup_app(tmp_path, "auth:\n  session_max_days: 7\n")
        resp = client.post("/api/auth/login", json=_LOGIN, headers=_H)
        assert 6.9 < _session_days(db_path, resp) < 7.1

    @pytest.mark.parametrize(("mode", "days"), [("true", 30), (None, 365)])
    def test_2fa_verify_session_expiry(self, tmp_path, monkeypatch, mode, days):
        if mode:
            monkeypatch.setenv("DANGO_CLOUD_MODE", mode)
        else:
            monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        client, db_path = _setup_app(
            tmp_path,
            "auth:\n  session_max_days: soon\n",
            totp_enabled=True,
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        login = client.post("/api/auth/login", json=_LOGIN, headers=_H)
        assert login.json()["requires_2fa"] is True
        partial = login.headers["set-cookie"].split(f"{COOKIE_NAME}=")[1].split(";")[0]
        client.cookies.clear()
        client.cookies.set(COOKIE_NAME, partial)
        with patch("dango.web.routes.auth_2fa.verify_totp_code", return_value=True):
            resp = client.post("/api/auth/2fa/verify", json={"code": "123456"}, headers=_H)
        assert resp.status_code == 200
        assert days - 0.1 < _session_days(db_path, resp) < days + 0.1
