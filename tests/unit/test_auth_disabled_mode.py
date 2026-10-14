"""tests/unit/test_auth_disabled_mode.py

Explicitly disabled auth (local only) passes require_permission, except users.manage; errors never disable auth.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.web.routes.dbt import router as dbt_router
from dango.web.routes.users import router as users_router
from tests.factories.auth_app_factories import (
    INVALID_YAML,
    AuthProject,
    build_client,
    find_auth_middleware,
)

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_RUN_MODEL = "/api/dbt/models/orders/run"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    p = AuthProject(tmp_path)
    p.create_db()
    return p


@pytest.fixture
def no_manifest():
    """Permission passes -> the handler answers 404 (no manifest) without running dbt."""
    with patch("dango.web.routes.dbt.get_dbt_manifest", return_value={}) as m:
        yield m


@pytest.mark.unit
class TestAuthDisabledMode:
    def test_auth_disabled_local_passes_require_permission(
        self, project: AuthProject, no_manifest: object
    ) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root, dbt_router)

        resp = client.post(_RUN_MODEL, headers=_CSRF)

        assert resp.status_code != 401
        assert resp.status_code == 404

    def test_auth_disabled_sets_flag_but_no_user(self, project: AuthProject) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root)

        body = client.get("/api/probe").json()

        assert body["auth_disabled"] is True
        assert body["user"] is None

    def test_auth_disabled_on_cloud_still_requires_user(
        self, project: AuthProject, monkeypatch: pytest.MonkeyPatch, no_manifest: object
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        project.write_toggle("enabled: false\n")
        client = build_client(project.root, dbt_router)

        assert client.post(_RUN_MODEL, headers=_CSRF).status_code == 401
        assert client.get("/api/probe").json()["auth_disabled"] is False

    def test_toggle_error_never_sets_auth_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_manifest: object
    ) -> None:
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        project = AuthProject(tmp_path)  # local, no auth.db: fail-closed path stays open
        project.write_toggle(INVALID_YAML)
        client = build_client(project.root, dbt_router)

        assert client.post(_RUN_MODEL, headers=_CSRF).status_code == 401
        assert client.get("/api/probe").json()["auth_disabled"] is False

    def test_auth_enabled_anonymous_still_401(
        self, project: AuthProject, no_manifest: object
    ) -> None:
        project.write_toggle("enabled: true\n")
        client = build_client(project.root, dbt_router)

        assert client.post(_RUN_MODEL, headers=_CSRF).status_code == 401

    def test_auth_disabled_does_not_grant_users_manage(self, project: AuthProject) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root, users_router)

        created = client.post(
            "/api/admin/users",
            json={"email": "intruder@example.com", "role": "admin"},
            headers=_CSRF,
        )
        listed = client.get("/api/admin/users")

        assert created.status_code == 401
        assert listed.status_code == 401

    def test_cache_reset_to_none_rereads_toggle(self, project: AuthProject) -> None:
        project.write_toggle("enabled: false\n")
        client = build_client(project.root)
        assert client.get("/api/probe").json()["auth_disabled"] is True
        mw = find_auth_middleware(client)

        project.write_toggle("enabled: true\n")
        mw._auth_enabled_cache = None
        assert client.get("/api/probe").status_code == 401

        project.write_toggle("enabled: false\n")
        mw._auth_enabled_cache = None
        resp = client.get("/api/probe")
        assert resp.status_code == 200
        assert resp.json()["auth_disabled"] is True
