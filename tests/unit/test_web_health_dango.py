"""tests/unit/test_web_health_dango.py

Public GET /api/health reports Dango's own state; Metabase and Docker are bounded, cached component states.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import yaml
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from dango.web.app import app as real_app
from tests.factories.auth_app_factories import AuthProject, build_client

_DOCKER_PS = "dango.platform.docker.DockerManager.is_docker_daemon_running"
_USES_HELPER = "dango.platform.compose_exec.uses_helper"
_SECRET = "s3cr3t-token-9f2c"
_SECRET_PATH = "/Users/someone/private-project/.dango/secrets"
_TIME_BOUND_SECONDS = 2.0
_METABASE_STATES = {"ok", "starting", "unavailable", "unknown"}
_DOCKER_STATES = {"ok", "unavailable", "not_applicable", "unknown"}


class MetabaseDouble:
    """A local stand-in for Metabase's /api/health on an ephemeral port."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.hits = 0
        self.seen_headers: list[dict[str, str]] = []
        self.release = threading.Event()
        self._lock = threading.Lock()
        double = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with double._lock:
                    double.hits += 1
                    double.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                if double.mode == "hang":
                    double.release.wait(30)
                    return
                if double.mode == "ok":
                    status, body = 200, {"status": "ok"}
                elif double.mode == "starting":
                    status, body = 503, {"status": "initializing", "progress": 0.4}
                else:
                    status, body = 500, {"error": f"{_SECRET} at {_SECRET_PATH}"}
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def make_double() -> Iterator[Callable[[str], MetabaseDouble]]:
    doubles: list[MetabaseDouble] = []

    def factory(mode: str) -> MetabaseDouble:
        doubles.append(MetabaseDouble(mode))
        return doubles[-1]

    yield factory
    for double in doubles:
        double.close()


@pytest.fixture(autouse=True)
def docker_is_faked() -> Iterator[None]:
    """No test may reach a real Docker daemon: the probe is always a fake."""
    with patch(_DOCKER_PS, return_value=True), patch(_USES_HELPER, return_value=False):
        yield


def _project(tmp_path: Path, metabase_url: str) -> Path:
    root = tmp_path / "proj"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": metabase_url}), encoding="utf-8"
    )
    return root


@pytest.fixture(autouse=True)
def restore_app_state() -> Iterator[None]:
    """The real app is a module singleton: give it what the server's lifespan would, then undo it."""
    missing = object()
    saved = getattr(real_app.state, "project_root", missing)
    # The lifespan creates this shared client for the Metabase proxy; tests run without it.
    real_app.state.http_client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
    yield
    asyncio.run(real_app.state.http_client.aclose())
    del real_app.state.http_client
    if saved is missing:
        if hasattr(real_app.state, "project_root"):
            del real_app.state.project_root
    else:
        real_app.state.project_root = saved


def _client(root: Path | None) -> TestClient:
    """A client on the real application (all routers registered) serving *root*."""
    real_app.state.project_root = root
    return TestClient(real_app, raise_server_exceptions=False)


def _closed_port_url() -> str:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.mark.unit
def test_route_table_serves_health_from_dango_not_the_proxy(tmp_path: Path) -> None:
    matches = [
        ctx.route
        for ctx in iter_route_contexts(real_app.routes)
        if getattr(ctx.route, "path", None) == "/api/health"
    ]
    assert len(matches) == 1
    assert matches[0].endpoint.__module__ == "dango.web.routes.health"


@pytest.mark.unit
def test_healthy_metabase_reports_ok(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    resp = _client(_project(tmp_path, double.url)).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "components": {"dango": "ok", "metabase": "ok", "docker": "ok"},
    }


@pytest.mark.unit
def test_initializing_metabase_reports_starting(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("starting")
    resp = _client(_project(tmp_path, double.url)).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["components"]["metabase"] == "starting"
    assert resp.json()["status"] == "ok"


@pytest.mark.unit
def test_refused_connection_is_a_component_state_not_an_error(tmp_path: Path) -> None:
    resp = _client(_project(tmp_path, _closed_port_url())).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["components"]["metabase"] == "unavailable"
    assert resp.json()["components"]["dango"] == "ok"


@pytest.mark.unit
def test_metabase_500_is_unavailable_and_its_text_is_not_echoed(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("error")
    resp = _client(_project(tmp_path, double.url)).get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["components"]["metabase"] == "unavailable"


@pytest.mark.unit
def test_hung_metabase_answers_within_the_time_bound(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("hang")
    client = _client(_project(tmp_path, double.url))
    started = time.monotonic()
    resp = client.get("/api/health")
    elapsed = time.monotonic() - started
    assert resp.status_code == 200
    assert resp.json()["components"]["metabase"] == "unavailable"
    assert elapsed < _TIME_BOUND_SECONDS, elapsed


@pytest.mark.unit
def test_hung_docker_answers_within_the_time_bound_as_unknown(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    gate = threading.Event()
    real_app.state.project_root = _project(tmp_path, double.url)

    def slow_docker(*_: Any, **__: Any) -> bool:
        gate.wait(10)
        return True

    async def timed_request() -> tuple[httpx.Response, float]:
        # One event loop for the whole call: the time measured is the response, not loop teardown.
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = time.monotonic()
            try:
                resp = await client.get("/api/health")
                return resp, time.monotonic() - started
            finally:
                gate.set()

    with patch(_DOCKER_PS, side_effect=slow_docker):
        resp, elapsed = asyncio.run(timed_request())
    assert resp.status_code == 200
    assert resp.json()["components"]["docker"] == "unknown"
    assert resp.json()["components"]["metabase"] == "ok"
    assert elapsed < _TIME_BOUND_SECONDS, elapsed


@pytest.mark.unit
def test_rapid_requests_probe_each_component_once_per_window(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    with patch(_DOCKER_PS, return_value=True) as docker_ps:
        bodies = [client.get("/api/health").json() for _ in range(10)]
    assert double.hits == 1
    assert docker_ps.call_count == 1
    assert all(body == bodies[0] for body in bodies)


@pytest.mark.unit
def test_cache_expires_and_probes_again(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    with (
        patch("dango.web.health_probe.METABASE_CACHE_SECONDS", 0.3),
        patch("dango.web.health_probe.DOCKER_CACHE_SECONDS", 0.3),
    ):
        assert client.get("/api/health").json()["components"]["metabase"] == "ok"
        double.mode = "starting"
        assert client.get("/api/health").json()["components"]["metabase"] == "ok"
        time.sleep(0.45)
        assert client.get("/api/health").json()["components"]["metabase"] == "starting"
    assert double.hits == 2


@pytest.mark.unit
def test_concurrent_requests_share_one_in_flight_probe(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("hang")
    client = _client(_project(tmp_path, double.url))
    results: list[int] = []

    def call() -> None:
        results.append(client.get("/api/health").status_code)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == [200] * 5
    assert double.hits == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("daemon_up", "helper", "expected"),
    [(True, False, "ok"), (False, False, "unavailable"), (False, True, "not_applicable")],
)
def test_docker_component_states(
    tmp_path: Path,
    make_double: Callable[[str], MetabaseDouble],
    daemon_up: bool,
    helper: bool,
    expected: str,
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    with patch(_DOCKER_PS, return_value=daemon_up), patch(_USES_HELPER, return_value=helper):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["components"]["docker"] == expected


@pytest.mark.unit
def test_docker_probe_crash_is_unknown_and_never_a_5xx(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    with patch(_DOCKER_PS, side_effect=RuntimeError(f"{_SECRET} {_SECRET_PATH}")):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["components"]["docker"] == "unknown"
    assert _SECRET not in resp.text and _SECRET_PATH not in resp.text


@pytest.mark.unit
def test_body_is_coarse_states_only_and_leaks_nothing(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("error")
    root = _project(tmp_path, double.url)
    client = _client(root)
    with patch(_DOCKER_PS, side_effect=RuntimeError(f"{_SECRET} {_SECRET_PATH}")):
        resp = client.get("/api/health")
    body = resp.json()
    assert set(body) == {"status", "components"}
    assert set(body["components"]) == {"dango", "metabase", "docker"}
    assert body["status"] in {"ok", "degraded"}
    assert body["components"]["metabase"] in _METABASE_STATES
    assert body["components"]["docker"] in _DOCKER_STATES
    everything = resp.text + json.dumps(dict(resp.headers))
    for needle in (_SECRET, _SECRET_PATH, str(double.port), str(root), "127.0.0.1", "version"):
        assert needle not in everything, needle


@pytest.mark.unit
def test_unresolvable_project_is_degraded_but_still_200() -> None:
    client = _client(None)
    with patch("dango.web.routes.health.get_project_root", side_effect=RuntimeError("no root")):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["dango"] == "degraded"
    assert "no root" not in resp.text


@pytest.mark.unit
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_health_is_get_only_and_never_probes_on_other_methods(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble], method: str
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    resp = client.request(method, "/api/health", content=b"{}")
    assert resp.status_code == 405
    assert double.hits == 0


@pytest.mark.unit
def test_head_and_options_without_origin_are_405_as_before(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    assert client.head("/api/health").status_code == 405
    assert client.options("/api/health").status_code == 405
    assert double.hits == 0


@pytest.mark.unit
def test_probe_sends_no_dango_credentials_to_metabase(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    double = make_double("ok")
    client = _client(_project(tmp_path, double.url))
    client.get(
        "/api/health",
        headers={"Cookie": "dango_session=D; metabase.SESSION=M", "Authorization": "Bearer k"},
    )
    assert len(double.seen_headers) == 1
    assert "cookie" not in double.seen_headers[0]
    assert "authorization" not in double.seen_headers[0]


@pytest.mark.unit
def test_only_the_basic_health_route_is_public_under_the_auth_middleware(
    tmp_path: Path, make_double: Callable[[str], MetabaseDouble]
) -> None:
    """The unauthenticated answer is reachable without credentials; the detailed routes are not."""
    from dango.web.middleware.auth import AuthMiddleware
    from dango.web.routes.health import router as health_router

    double = make_double("ok")
    root = _project(tmp_path, double.url)
    AuthProject(root).create_db()
    client = build_client(root, health_router)
    assert client.get("/api/health").status_code == 200
    middleware = AuthMiddleware(lambda *_: None, project_root=root, idle_timeout_minutes=60)
    assert middleware._is_public_route("/api/health")
    assert not middleware._is_public_route("/api/health/platform")
    assert not middleware._is_public_route("/api/status")


@pytest.mark.unit
def test_health_stays_in_the_public_and_rate_limit_allowlists() -> None:
    from dango.config.models import RateLimitConfig
    from dango.web.middleware.auth import _PUBLIC_EXACT
    from dango.web.middleware.rate_limit import RateLimitMiddleware

    assert "/api/health" in _PUBLIC_EXACT
    middleware = RateLimitMiddleware(lambda *_: None, RateLimitConfig())
    assert middleware._classify_route("/api/health") is None


@pytest.mark.unit
def test_metabase_proxy_router_no_longer_owns_api_health() -> None:
    from dango.web.routes.metabase_proxy import router

    assert "/api/health" not in {getattr(r, "path", None) for r in router.routes}


@pytest.mark.unit
def test_response_budget_fits_inside_dango_start_poll_timeout() -> None:
    """`dango start` polls /api/health with a 1 second client timeout."""
    from dango.web import health_probe

    assert health_probe.RESPONSE_BUDGET_SECONDS <= 0.9
