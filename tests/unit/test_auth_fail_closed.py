"""tests/unit/test_auth_fail_closed.py

AuthMiddleware fails closed when .dango/auth.yml cannot be read, and on cloud when auth.db is missing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.web.routes.config import router as config_router
from tests.factories.auth_app_factories import (
    INVALID_YAML,
    AuthProject,
    build_client,
    find_auth_middleware,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    return AuthProject(tmp_path)


@pytest.mark.unit
class TestCorruptToggle:
    def test_corrupt_auth_yml_fails_closed_when_auth_db_exists(self, project: AuthProject) -> None:
        project.create_db()
        project.write_toggle(INVALID_YAML)
        client = build_client(project.root, config_router)

        resp = client.get("/api/config")

        assert resp.status_code == 401

    def test_corrupt_auth_yml_fails_closed_in_cloud_mode(
        self, project: AuthProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        project.write_toggle(INVALID_YAML)  # no auth.db yet
        client = build_client(project.root, config_router)

        resp = client.get("/api/config")

        assert resp.status_code in (401, 503)

    def test_corrupt_auth_yml_local_without_auth_db_stays_open(self, project: AuthProject) -> None:
        """Auth was never configured locally: a broken toggle file does not lock the user out."""
        project.write_toggle(INVALID_YAML)
        client = build_client(project.root)

        resp = client.get("/api/probe")

        assert resp.status_code == 200

    def test_restore_takes_effect_without_cache_delay(self, project: AuthProject) -> None:
        project.create_db()
        user = project.add_user()
        cookie = project.session_cookie(user)
        project.write_toggle(INVALID_YAML)
        client = build_client(project.root)

        # Corrupt: anonymous refused, a valid session still works, nothing cached.
        assert client.get("/api/probe").status_code == 401
        resp = client.get("/api/probe", headers={"Cookie": cookie})
        assert resp.status_code == 200
        assert resp.json()["auth_disabled"] is False
        mw = find_auth_middleware(client)
        assert mw._auth_enabled_cache is None

        # Restored within the cache TTL: the next request reads the valid file.
        project.write_toggle("enabled: true\n")
        assert client.get("/api/probe").status_code == 401
        resp = client.get("/api/probe", headers={"Cookie": cookie})
        assert resp.status_code == 200
        assert resp.json()["user"] == user.email
        assert resp.json()["auth_disabled"] is False
        assert mw._auth_enabled_cache is True

    def test_toggle_read_error_logged_once_at_error_level(self, project: AuthProject) -> None:
        project.create_db()
        project.write_toggle(INVALID_YAML)
        client = build_client(project.root)

        with patch("dango.web.middleware.auth.logger") as mock_logger:
            client.get("/api/probe")
            client.get("/api/probe")

        error_events = [c.args[0] for c in mock_logger.error.call_args_list]
        assert error_events == ["auth_toggle_read_failed"]


@pytest.mark.unit
class TestMissingAuthDb:
    def test_missing_auth_db_in_cloud_mode_returns_503(
        self, project: AuthProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        project.write_toggle("enabled: true\n")
        client = build_client(project.root, config_router)

        resp = client.get("/api/config")

        assert resp.status_code == 503
        assert ".dango" not in resp.text
        assert str(project.root) not in resp.text

    def test_missing_auth_db_local_unchanged(self, project: AuthProject) -> None:
        project.write_toggle("enabled: true\n")
        client = build_client(project.root)

        resp = client.get("/api/probe")

        assert resp.status_code == 200


@pytest.mark.unit
class TestErrorCodeCatalog:
    def test_auth_db_unavailable_code_is_registered(
        self, project: AuthProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango import exceptions

        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        project.write_toggle("enabled: true\n")
        client = build_client(project.root)

        code = client.get("/api/probe").json()["error_code"]

        registered = {
            getattr(obj, "_default_error_code", None)
            for name in exceptions.__all__
            if isinstance(obj := getattr(exceptions, name), type)
        }
        assert code == "DANGO-S009"
        assert {"DANGO-S008", "DANGO-S009"} <= registered

    def test_error_codes_are_unique(self) -> None:
        from dango import exceptions

        codes = [
            obj.__dict__["_default_error_code"]
            for name in exceptions.__all__
            if isinstance(obj := getattr(exceptions, name), type)
            and "_default_error_code" in obj.__dict__
        ]
        assert len(codes) == len(set(codes))
