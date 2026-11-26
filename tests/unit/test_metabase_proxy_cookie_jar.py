"""tests/unit/test_metabase_proxy_cookie_jar.py

The Metabase proxy's shared HTTP client never remembers cookies between requests: a cookie set by
Metabase on one request is never attached to a later request from another user. A real local
upstream records every Cookie header it receives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml
from fastapi import FastAPI

from dango.web.proxy_client import make_proxy_client
from dango.web.routes.metabase_proxy import router as proxy_router
from tests.factories.fake_metabase_cookies import FakeMetabase

_REBRIDGE = "dango.web.routes.metabase_proxy._rebridge_if_needed"
_DANGO_B = "dango_session=DANGO-B"
_DANGO_C = "dango_session=DANGO-C"


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMetabase]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    fake = FakeMetabase()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


def _new_proxy_client() -> httpx.AsyncClient:
    """The proxy's shared client, built by the same factory as the server's lifespan."""
    return make_proxy_client()


@asynccontextmanager
async def _dango(tmp_path: Path, upstream: FakeMetabase) -> AsyncIterator[httpx.AsyncClient]:
    """The real proxy routes behind a signed-in user; every request sets its own Cookie header."""
    (tmp_path / ".dango").mkdir(exist_ok=True)
    (tmp_path / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": upstream.url}), encoding="utf-8"
    )
    app = FastAPI()
    app.state.project_root = tmp_path
    app.state.http_client = _new_proxy_client()
    user = MagicMock()
    user.email = "proxy@example.com"

    @app.middleware("http")
    async def set_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(proxy_router)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://dango.test") as browser:
            yield browser
    finally:
        await app.state.http_client.aclose()


def _run(
    tmp_path: Path,
    upstream: FakeMetabase,
    scenario: Callable[[httpx.AsyncClient], Awaitable[None]],
) -> None:
    async def main() -> None:
        async with _dango(tmp_path, upstream) as browser:
            await scenario(browser)

    asyncio.run(main())


async def _login(browser: httpx.AsyncClient, query: str = "") -> httpx.Response:
    """User A logs in to Metabase through the proxy, as the Metabase login page does."""
    return await browser.post(
        f"/api/session{query}",
        content=b'{"username":"a@example.com","password":"x"}',
        headers={"Content-Type": "application/json", "Cookie": "dango_session=DANGO-A"},
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/api/user", "/api/database", "/app/main.js"])
def test_request_without_a_session_never_receives_another_users_login_cookies(
    tmp_path: Path, upstream: FakeMetabase, path: str
) -> None:
    async def scenario(browser: httpx.AsyncClient) -> None:
        assert (await _login(browser)).status_code == 200
        resp = await browser.get(f"{path}?who=b", headers={"Cookie": _DANGO_B})
        assert resp.status_code == 401

    _run(tmp_path, upstream, scenario)

    (seen,) = upstream.requests_for("b")
    assert seen.cookies == []


@pytest.mark.unit
def test_login_response_cookies_still_reach_the_browser(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(browser: httpx.AsyncClient) -> None:
        resp = await _login(browser)
        assert resp.status_code == 200
        assert resp.json() == {"id": "sess-1"}
        set_cookies = resp.headers.get_list("set-cookie")
        assert [c.split("=")[0] for c in set_cookies] == ["metabase.DEVICE", "metabase.TIMEOUT"]

    _run(tmp_path, upstream, scenario)


@pytest.mark.unit
def test_a_request_with_its_own_session_forwards_exactly_that_cookie(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(browser: httpx.AsyncClient) -> None:
        await _login(browser)
        resp = await browser.get(
            "/api/user?who=b",
            headers={"Cookie": f"{_DANGO_B}; metabase.SESSION=user-b-sess; other=x"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"session": "user-b-sess"}

    _run(tmp_path, upstream, scenario)

    (seen,) = upstream.requests_for("b")
    assert seen.cookies == ["metabase.SESSION=user-b-sess"]


@pytest.mark.unit
def test_bridge_injected_session_is_forwarded_and_set_on_the_browser(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(browser: httpx.AsyncClient) -> None:
        await _login(browser)
        with patch(_REBRIDGE, return_value="bridged-sess"):
            resp = await browser.get("/metabase/api/user?who=b", headers={"Cookie": _DANGO_B})
        assert resp.status_code == 200
        assert "metabase.SESSION=bridged-sess" in resp.headers.get("set-cookie", "")

    _run(tmp_path, upstream, scenario)

    (seen,) = upstream.requests_for("b")
    assert seen.cookies == ["metabase.SESSION=bridged-sess"]


@pytest.mark.unit
def test_rebridge_after_a_401_retries_with_only_the_fresh_session(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(browser: httpx.AsyncClient) -> None:
        await _login(browser)
        with patch(_REBRIDGE, return_value="fresh-sess"):
            resp = await browser.get(
                "/api/user?who=b", headers={"Cookie": f"{_DANGO_B}; metabase.SESSION=stale"}
            )
        assert resp.status_code == 200

    _run(tmp_path, upstream, scenario)

    assert [r.cookies for r in upstream.requests_for("b")] == [
        ["metabase.SESSION=stale"],
        ["metabase.SESSION=fresh-sess"],
    ]


@pytest.mark.unit
@pytest.mark.parametrize("order", ["login_first", "readers_first"])
def test_concurrent_users_never_see_each_others_cookies(
    tmp_path: Path, upstream: FakeMetabase, order: str
) -> None:
    """A login whose body is still arriving while two other users make requests."""

    async def scenario(browser: httpx.AsyncClient) -> None:
        async def reader(who: str, cookie: str, delay: str) -> httpx.Response:
            return await browser.get(
                f"/api/user?who={who}&delay={delay}", headers={"Cookie": cookie}
            )

        if order == "login_first":
            login = asyncio.create_task(_login(browser, "?slow=0.5"))
            await asyncio.sleep(0.2)
            b, c = await asyncio.gather(
                reader("b", _DANGO_B, "0"),
                reader("c", f"{_DANGO_C}; metabase.SESSION=user-c-sess", "0"),
            )
        else:
            readers = asyncio.gather(
                reader("b", _DANGO_B, "0.3"),
                reader("c", f"{_DANGO_C}; metabase.SESSION=user-c-sess", "0.3"),
            )
            await asyncio.sleep(0.1)
            login = asyncio.create_task(_login(browser, "?slow=0.1"))
            b, c = await readers
        assert (await login).status_code == 200
        assert b.status_code == 401
        assert c.json() == {"session": "user-c-sess"}

    _run(tmp_path, upstream, scenario)

    assert [r.cookies for r in upstream.requests_for("b")] == [[]]
    assert [r.cookies for r in upstream.requests_for("c")] == [["metabase.SESSION=user-c-sess"]]
