"""tests/unit/test_web_cors_origins.py

Credentialed CORS on the real app only allows Dango's own origins (local) or the cloud origin (cloud).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.web.app import app as global_app
from dango.web.app import create_app
from tests.factories.auth_app_factories import AuthProject

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_PROBE = "/api/probe"
_FOREIGN_ORIGINS = [
    "http://evil.example",
    "https://evil.example",
    "http://localhost:9999",
    "http://127.0.0.1:9999",
    "http://[::1]:9999",
    "null",
]
_ACAO = "access-control-allow-origin"
_ACAC = "access-control-allow-credentials"


@pytest.fixture(autouse=True)
def _local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


class _Project:
    """A real project directory (auth.db + one user) and the app built from it."""

    def __init__(self, root: Path) -> None:
        self.auth = AuthProject(root)
        self.auth.create_db()
        self.auth.write_toggle("enabled: true\n")
        self.cookie = self.auth.session_cookie(self.auth.add_user())
        self.app: FastAPI = create_app(root)

        @self.app.get(_PROBE)
        async def _read() -> dict[str, str]:
            return {"projects": "private-data"}

        @self.app.post(_PROBE)
        async def _write() -> dict[str, str]:
            return {"changed": "yes"}

    def write_cloud_yml(self, text: str) -> None:
        (self.auth.root / ".dango" / "cloud.yml").write_text(text, encoding="utf-8")

    def client(self, host: str = "localhost:8800") -> TestClient:
        # The Host header is set explicitly: the test client cannot parse an IPv6 base_url
        return TestClient(
            self.app,
            base_url="http://localhost:8800",
            headers={"Host": host},
            raise_server_exceptions=False,
            client=("127.0.0.1", 50000),
        )


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


def _preflight(client: TestClient, origin: str, path: str = _PROBE, **extra: str):
    headers = {"Origin": origin, "Access-Control-Request-Method": "GET", **extra}
    return client.options(path, headers=headers)


def _assert_no_cors_grant(headers) -> None:
    assert _ACAO not in headers
    assert _ACAC not in headers


@pytest.mark.unit
class TestForeignOriginsLocalMode:
    @pytest.mark.parametrize("origin", _FOREIGN_ORIGINS)
    def test_preflight_from_foreign_origin_gets_no_grant(
        self, project: _Project, origin: str
    ) -> None:
        resp = _preflight(project.client(), origin)

        _assert_no_cors_grant(resp.headers)

    @pytest.mark.parametrize("origin", _FOREIGN_ORIGINS)
    def test_credentialed_read_from_foreign_origin_has_no_cors_headers(
        self, project: _Project, origin: str
    ) -> None:
        resp = project.client().get(_PROBE, headers={"Origin": origin, "Cookie": project.cookie})

        # The server cannot stop a same-site request: the data is returned. What is missing
        # is Access-Control-Allow-Origin, which is what makes a browser withhold it from the page.
        assert resp.status_code == 200
        assert resp.json() == {"projects": "private-data"}
        _assert_no_cors_grant(resp.headers)

    def test_foreign_preflight_for_a_real_route_on_the_global_app(self) -> None:
        client = TestClient(global_app, base_url="http://localhost:8800")

        resp = _preflight(client, "http://localhost:9999", path="/api/sources")

        _assert_no_cors_grant(resp.headers)

    def test_wildcard_is_never_granted(self, project: _Project) -> None:
        resp = _preflight(project.client(), "http://evil.example")

        assert resp.headers.get(_ACAO) != "*"


@pytest.mark.unit
class TestOwnOriginsLocalMode:
    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            ("localhost:8800", "http://localhost:8800"),
            ("localhost:8800", "http://127.0.0.1:8800"),
            ("localhost:8800", "http://[::1]:8800"),
            ("127.0.0.1:8800", "http://localhost:8800"),
            ("127.0.0.1:8800", "http://127.0.0.1:8800"),
            ("[::1]:8800", "http://[::1]:8800"),
            ("localhost:18123", "http://127.0.0.1:18123"),
            ("localhost", "http://localhost"),
        ],
    )
    def test_own_origin_preflight_is_reflected_with_credentials(
        self, project: _Project, host: str, origin: str
    ) -> None:
        resp = _preflight(project.client(host), origin)

        assert resp.status_code == 200
        assert resp.headers[_ACAO] == origin
        assert resp.headers[_ACAC] == "true"

    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            ("localhost:8800", "http://localhost:8800"),
            ("localhost:18123", "http://127.0.0.1:18123"),
            ("[::1]:8800", "http://[::1]:8800"),
        ],
    )
    def test_own_origin_read_is_reflected_with_credentials(
        self, project: _Project, host: str, origin: str
    ) -> None:
        resp = project.client(host).get(
            _PROBE, headers={"Origin": origin, "Cookie": project.cookie}
        )

        assert resp.status_code == 200
        assert resp.headers[_ACAO] == origin
        assert resp.headers[_ACAC] == "true"

    def test_port_follows_the_request_not_a_fixed_default(self, project: _Project) -> None:
        # Own origin on 8800 is foreign for an app reached on 18123, and the reverse
        assert (
            _ACAO
            not in _preflight(project.client("localhost:18123"), "http://localhost:8800").headers
        )
        assert (
            _ACAO
            not in _preflight(project.client("localhost:8800"), "http://localhost:18123").headers
        )

    def test_https_origin_on_own_port_is_not_granted(self, project: _Project) -> None:
        resp = _preflight(project.client("localhost:8800"), "https://localhost:8800")

        _assert_no_cors_grant(resp.headers)

    def test_garbage_host_header_grants_nothing(self, project: _Project) -> None:
        resp = _preflight(project.client(), "http://localhost:8800", Host="localhost:notaport")

        _assert_no_cors_grant(resp.headers)

    def test_same_origin_post_with_csrf_header_works(self, project: _Project) -> None:
        resp = project.client().post(
            _PROBE,
            headers={**_CSRF, "Origin": "http://localhost:8800", "Cookie": project.cookie},
        )

        assert resp.status_code == 200
        assert resp.headers[_ACAO] == "http://localhost:8800"

    def test_post_without_csrf_header_still_refused(self, project: _Project) -> None:
        resp = project.client().post(
            _PROBE, headers={"Origin": "http://localhost:8800", "Cookie": project.cookie}
        )

        assert resp.status_code == 403

    def test_preflight_allows_the_csrf_header_and_unsafe_methods(self, project: _Project) -> None:
        resp = _preflight(
            project.client(),
            "http://localhost:8800",
            **{
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "x-requested-with, content-type",
            },
        )

        assert resp.status_code == 200
        assert "DELETE" in resp.headers["access-control-allow-methods"]
        assert resp.headers["access-control-allow-headers"] == "x-requested-with, content-type"

    def test_responses_vary_on_origin(self, project: _Project) -> None:
        own = project.client().get(
            _PROBE, headers={"Origin": "http://localhost:8800", "Cookie": project.cookie}
        )
        foreign = project.client().get(
            _PROBE, headers={"Origin": "http://evil.example", "Cookie": project.cookie}
        )

        assert "Origin" in own.headers["vary"]
        assert "Origin" in foreign.headers["vary"]

    def test_own_origin_preflight_on_the_global_app(self) -> None:
        client = TestClient(global_app, base_url="http://127.0.0.1:8800")

        resp = _preflight(client, "http://localhost:8800", path="/api/sources")

        assert resp.headers[_ACAO] == "http://localhost:8800"
        assert resp.headers[_ACAC] == "true"


@pytest.mark.unit
class TestNoOriginClients:
    def test_request_without_origin_is_untouched(self, project: _Project) -> None:
        resp = project.client().get(_PROBE, headers={"Cookie": project.cookie})

        assert resp.status_code == 200
        assert resp.json() == {"projects": "private-data"}
        _assert_no_cors_grant(resp.headers)

    def test_api_key_client_without_origin_works(self, project: _Project) -> None:
        user = project.auth.add_user("agent@example.com")
        key = project.auth.api_key(user)

        resp = project.client().get(_PROBE, headers={"Authorization": f"Bearer {key}"})

        assert resp.status_code == 200


@pytest.mark.unit
class TestCloudMode:
    @pytest.mark.parametrize(
        ("yaml_text", "cloud_origin"),
        [
            ("droplet_ip: 203.0.113.7\ndomain: data.example.org\n", "https://data.example.org"),
            ("droplet_ip: 203.0.113.7\n", "http://203.0.113.7"),
        ],
        ids=["domain", "ip-only"],
    )
    def test_only_the_cloud_origin_is_allowed(
        self, project: _Project, yaml_text: str, cloud_origin: str
    ) -> None:
        project.write_cloud_yml(yaml_text)
        app_client = TestClient(
            create_cloud_app(project),
            base_url="http://localhost:8800",
            client=("127.0.0.1", 50000),
        )

        allowed = _preflight(app_client, cloud_origin)
        assert allowed.headers[_ACAO] == cloud_origin
        assert allowed.headers[_ACAC] == "true"
        for origin in ("http://localhost:8800", "http://evil.example"):
            _assert_no_cors_grant(_preflight(app_client, origin).headers)

    def test_cloud_origin_read_is_reflected_and_foreign_read_is_not(
        self, project: _Project
    ) -> None:
        project.write_cloud_yml("droplet_ip: 203.0.113.7\ndomain: data.example.org\n")
        app_client = TestClient(
            create_cloud_app(project),
            base_url="https://data.example.org",
            client=("127.0.0.1", 50000),
        )

        ok = app_client.get(
            _PROBE, headers={"Origin": "https://data.example.org", "Cookie": project.cookie}
        )
        other = app_client.get(
            _PROBE, headers={"Origin": "http://localhost:9999", "Cookie": project.cookie}
        )

        assert ok.headers[_ACAO] == "https://data.example.org"
        _assert_no_cors_grant(other.headers)

    @pytest.mark.parametrize(
        "origin", ["http://localhost:8800", "http://localhost:9999", "http://evil.example"]
    )
    def test_unreadable_cloud_config_fails_closed(self, project: _Project, origin: str) -> None:
        project.write_cloud_yml("droplet_ip: [unterminated\n")
        app_client = TestClient(create_cloud_app(project), base_url="http://localhost:8800")

        _assert_no_cors_grant(_preflight(app_client, origin).headers)

    @pytest.mark.parametrize(
        "origin", ["http://localhost:8800", "http://localhost:9999", "http://evil.example"]
    )
    def test_cloud_server_without_a_cloud_config_fails_closed(
        self, project: _Project, monkeypatch: pytest.MonkeyPatch, origin: str
    ) -> None:
        # On the server itself DANGO_CLOUD_MODE is set and cloud.yml is not synced there
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        app_client = TestClient(create_cloud_app(project), base_url="http://localhost:8800")

        _assert_no_cors_grant(_preflight(app_client, origin).headers)


def create_cloud_app(project: _Project) -> FastAPI:
    """Build the app again (the CORS origins are decided at creation) with the probe routes."""
    application = create_app(project.auth.root)

    @application.get(_PROBE)
    async def _read() -> dict[str, str]:
        return {"projects": "private-data"}

    return application
