"""tests/unit/test_g4f2_cloud_auth_toggle.py

On a cloud process the auth middleware never treats a missing, empty or false local toggle as
"auth off"; local-mode behaviour is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.auth_app_factories import AuthProject, build_client


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    return AuthProject(tmp_path)


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANGO_CLOUD_MODE", "true")


@pytest.mark.unit
@pytest.mark.usefixtures("cloud")
class TestCloudRequiresAuthWhateverTheToggleSays:
    @pytest.mark.parametrize(
        "toggle",
        [None, "", "{}\n", "other: 1\n", "enabled: false\n"],
        ids=["missing", "empty", "empty-mapping", "no-enabled-key", "disabled"],
    )
    def test_unauthenticated_request_is_refused(
        self, project: AuthProject, toggle: str | None
    ) -> None:
        project.create_db()
        if toggle is not None:
            project.write_toggle(toggle)
        resp = build_client(project.root).get("/api/probe")
        assert resp.status_code == 401

    def test_missing_toggle_and_missing_auth_db_returns_503(self, project: AuthProject) -> None:
        resp = build_client(project.root).get("/api/probe")
        assert resp.status_code == 503

    def test_valid_credentials_still_pass_with_a_missing_toggle(self, project: AuthProject) -> None:
        project.create_db()
        user = project.add_user()
        resp = build_client(project.root).get(
            "/api/probe", headers={"Cookie": project.session_cookie(user)}
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == user.email
        assert resp.json()["auth_disabled"] is False


@pytest.mark.unit
class TestLocalModeUnchanged:
    @pytest.mark.parametrize("toggle", [None, "enabled: false\n"], ids=["missing", "disabled"])
    def test_local_process_with_auth_off_stays_open(
        self, project: AuthProject, toggle: str | None
    ) -> None:
        if toggle is not None:
            project.write_toggle(toggle)
        resp = build_client(project.root).get("/api/probe")
        assert resp.status_code == 200
        assert resp.json()["auth_disabled"] is True
