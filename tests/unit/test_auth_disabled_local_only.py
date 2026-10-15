"""tests/unit/test_auth_disabled_local_only.py

Auth-disabled mode needs a loopback TCP peer and a local Host (loopback or the project's nginx domain).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.web.routes.dbt import router as dbt_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_RUN_MODEL = "/api/dbt/models/orders/run"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    p = AuthProject(tmp_path / "proj")
    p.create_db()
    p.write_toggle("enabled: false\n")
    return p


@pytest.fixture(autouse=True)
def no_manifest():
    """Permission passes -> the handler answers 404 (no manifest) without running dbt."""
    with patch("dango.web.routes.dbt.get_dbt_manifest", return_value={}) as m:
        yield m


def _register_nginx_domain(home: Path, project_path: Path, domain: str) -> None:
    routing = {
        "version": "1.0",
        "port": 80,
        "projects": {
            "proj": {
                "domain": domain,
                "backend_port": 8800,
                "project_path": str(project_path),
                "status": "running",
            }
        },
        "next_available_port": 8801,
    }
    (home / ".dango").mkdir(exist_ok=True)
    (home / ".dango" / "routing.json").write_text(json.dumps(routing))


@pytest.mark.unit
class TestAuthDisabledLocalOnly:
    def test_dns_rebinding_host_refused(self, project: AuthProject) -> None:
        client = build_client(project.root, dbt_router)
        rebind = "rebind.attacker.example:8800"

        resp = client.post(
            _RUN_MODEL, headers={**_CSRF, "host": rebind, "origin": f"http://{rebind}"}
        )

        assert resp.status_code == 401

    def test_lan_peer_without_origin_refused(self, project: AuthProject) -> None:
        client = build_client(project.root, dbt_router, peer=("192.168.1.5", 50000))

        resp = client.post(_RUN_MODEL, headers={**_CSRF, "host": "192.168.1.10:8800"})

        assert resp.status_code == 401

    def test_lan_peer_spoofing_localhost_host_refused(self, project: AuthProject) -> None:
        client = build_client(project.root, dbt_router, peer=("192.168.1.5", 50000))

        resp = client.post(_RUN_MODEL, headers={**_CSRF, "host": "localhost:8800"})

        assert resp.status_code == 401

    def test_forwarded_for_does_not_count_as_peer(self, project: AuthProject) -> None:
        client = build_client(project.root, dbt_router, peer=("192.168.1.5", 50000))

        resp = client.post(
            _RUN_MODEL,
            headers={**_CSRF, "host": "localhost:8800", "x-forwarded-for": "127.0.0.1"},
        )

        assert resp.status_code == 401

    @pytest.mark.parametrize(
        ("peer", "host"),
        [
            (("127.0.0.1", 50000), "localhost:8800"),
            (("127.0.0.1", 50000), "127.0.0.1:8800"),
            (("::1", 50000), "[::1]:8800"),
        ],
        ids=["localhost", "ipv4-literal", "ipv6-literal"],
    )
    def test_localhost_direct_allowed(
        self, project: AuthProject, peer: tuple[str, int], host: str
    ) -> None:
        client = build_client(project.root, dbt_router, peer=peer)

        resp = client.post(_RUN_MODEL, headers={**_CSRF, "host": host})

        assert resp.status_code == 404  # passed the permission check

    def test_nginx_local_domain_from_loopback_allowed(
        self, project: AuthProject, tmp_path: Path
    ) -> None:
        _register_nginx_domain(tmp_path / "home", project.root, "proj.dango")
        client = build_client(project.root, dbt_router)

        resp = client.post(
            _RUN_MODEL,
            headers={**_CSRF, "host": "proj.dango", "origin": "http://proj.dango"},
        )

        assert resp.status_code == 404

    def test_other_projects_nginx_domain_refused(
        self, project: AuthProject, tmp_path: Path
    ) -> None:
        _register_nginx_domain(tmp_path / "home", tmp_path / "elsewhere", "other.dango")
        client = build_client(project.root, dbt_router)

        resp = client.post(
            _RUN_MODEL,
            headers={**_CSRF, "host": "other.dango", "origin": "http://other.dango"},
        )

        assert resp.status_code == 401
