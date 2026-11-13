"""tests/unit/test_rate_limit_behind_proxy.py

Regression guard: rate limiting uses the forwarded client address behind the bundled reverse proxy.

On cloud servers Caddy proxies every request to ``dango serve`` from 127.0.0.1.
``RateLimitMiddleware`` skips loopback clients, so it relies on uvicorn's
proxy-header handling (enabled by ``dango serve``) to replace the loopback peer
with the client address from ``X-Forwarded-For`` before the middleware runs.
These tests build the app through uvicorn's own ``Config.load()`` using the
proxy settings that ``dango serve`` actually passes to ``uvicorn.run``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from uvicorn.config import Config

from dango.config.models import RateLimitConfig
from dango.web.middleware.rate_limit import RateLimitMiddleware

_LOGIN_PATH = "/api/auth/login"
_STARTUP = "dango.platform.common.startup"
_SERVE = "dango.cli.commands.serve"


def _serve_uvicorn_kwargs(tmp_path: Any) -> dict[str, Any]:
    """Run ``dango serve`` with every side effect mocked; return the kwargs given to uvicorn.run."""
    from dango.cli.commands.serve import serve

    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    loader = MagicMock()
    loader.load_config.return_value = config

    with (
        patch("uvicorn.run") as mock_run,
        patch(f"{_SERVE}._check_port"),
        patch(f"{_SERVE}._stop_docker_quiet"),
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False),
        patch(f"{_STARTUP}.import_dashboards"),
        patch(f"{_STARTUP}.setup_metabase_if_needed", return_value={"success": True}),
        patch(f"{_STARTUP}.start_docker_services"),
        patch(f"{_STARTUP}.ensure_duckdb_driver"),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch("dango.config.ConfigLoader", return_value=loader),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
    ):
        result = CliRunner().invoke(serve, [], obj={})

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    return dict(mock_run.call_args.kwargs)


@pytest.fixture
def serve_proxy_kwargs(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The proxy-header settings ``dango serve`` passes to uvicorn."""
    # serve() writes DANGO_PROJECT_ROOT into os.environ; let monkeypatch restore it.
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    kwargs = _serve_uvicorn_kwargs(tmp_path)
    return {
        "proxy_headers": kwargs.get("proxy_headers", False),
        "forwarded_allow_ips": kwargs.get("forwarded_allow_ips"),
    }


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _build_stack(proxy_kwargs: dict[str, Any]) -> Any:
    """Wrap RateLimitMiddleware (default config) the way uvicorn does for ``dango serve``."""
    rate_limited = RateLimitMiddleware(_ok_app, RateLimitConfig())
    config = Config(rate_limited, **proxy_kwargs)
    config.load()
    return config.loaded_app


async def _post_login(app: Any, *, peer: str, xff: str | None) -> int:
    """Send one login POST through the ASGI stack and return the response status."""
    status: dict[str, int] = {}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    headers = [(b"x-forwarded-for", xff.encode("latin-1"))] if xff is not None else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _LOGIN_PATH,
        "raw_path": _LOGIN_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer, 54321),
        "server": ("127.0.0.1", 8800),
    }
    await app(scope, receive, send)
    return status["code"]


async def _statuses(app: Any, n: int, *, peer: str = "127.0.0.1", xff: str | None) -> list[int]:
    return [await _post_login(app, peer=peer, xff=xff) for _ in range(n)]


@pytest.mark.unit
class TestServeProxySettings:
    def test_serve_enables_proxy_headers_for_loopback(self, serve_proxy_kwargs):
        """``dango serve`` trusts X-Forwarded-For only from the local reverse proxy."""
        assert serve_proxy_kwargs == {
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1",
        }


@pytest.mark.unit
class TestRateLimitBehindProxy:
    """Default login limit is 10 per window; the 11th attempt must get 429."""

    @pytest.mark.anyio
    async def test_forwarded_client_limited_on_eleventh_attempt(self, serve_proxy_kwargs):
        app = _build_stack(serve_proxy_kwargs)
        statuses = await _statuses(app, 11, xff="203.0.113.9")
        assert statuses == [200] * 10 + [429]

    @pytest.mark.anyio
    async def test_forwarded_clients_limited_independently(self, serve_proxy_kwargs):
        app = _build_stack(serve_proxy_kwargs)
        first = await _statuses(app, 11, xff="203.0.113.9")
        second = await _statuses(app, 10, xff="198.51.100.7")
        assert first[-1] == 429
        assert second == [200] * 10

    @pytest.mark.anyio
    async def test_loopback_without_forwarded_header_skipped(self, serve_proxy_kwargs):
        """Local callers on the server (no proxy header) are not rate limited."""
        app = _build_stack(serve_proxy_kwargs)
        statuses = await _statuses(app, 15, xff=None)
        assert statuses == [200] * 15

    @pytest.mark.anyio
    async def test_spoofed_loopback_in_forwarded_header_still_limited(self, serve_proxy_kwargs):
        """A client-supplied loopback entry with the proxy-appended real address is limited."""
        app = _build_stack(serve_proxy_kwargs)
        statuses = await _statuses(app, 11, xff="127.0.0.1, 203.0.113.10")
        assert statuses == [200] * 10 + [429]
