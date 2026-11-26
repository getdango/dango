"""tests/unit/test_metabase_proxy_auth_off_session.py

With sign-in turned off locally, a manual Metabase login through the proxy keeps its session in that
browser's own cookie; with sign-in on, or from a non-local request, the upstream session cookie is
never handed to the browser. Real AuthMiddleware and a real local upstream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import yaml
from fastapi import FastAPI

from dango.web.middleware.auth import AuthMiddleware
from dango.web.proxy_client import make_proxy_client
from dango.web.routes.metabase_proxy import router as proxy_router
from tests.factories.auth_app_factories import AuthProject
from tests.factories.fake_metabase_cookies import FakeMetabase

_REBRIDGE = "dango.web.routes.metabase_proxy._rebridge_if_needed"
_LOGIN = {"content": b'{"username":"a@example.com","password":"x"}'}
_LOCAL = ("127.0.0.1", 50000)
_REMOTE = ("203.0.113.9", 50000)

NewBrowser = Callable[..., httpx.AsyncClient]


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


@asynccontextmanager
async def _dango(root: Path, upstream: FakeMetabase, *, auth_on: bool) -> AsyncIterator[NewBrowser]:
    """The real proxy routes behind the real AuthMiddleware; yields a factory of fresh browsers."""
    project = AuthProject(root)
    project.write_toggle("enabled: true\n" if auth_on else "enabled: false\n")
    if auth_on:
        project.create_db()
    (root / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": upstream.url}), encoding="utf-8"
    )
    app = FastAPI()
    app.state.project_root = root
    app.state.http_client = make_proxy_client()
    app.add_middleware(AuthMiddleware, project_root=root, idle_timeout_minutes=60)
    app.include_router(proxy_router)
    async with AsyncExitStack() as stack:

        def new_browser(
            peer: tuple[str, int] = _LOCAL, cookie: str | None = None
        ) -> httpx.AsyncClient:
            headers = {"Cookie": cookie} if cookie else {}
            transport = httpx.ASGITransport(app=app, client=peer)
            browser = httpx.AsyncClient(
                transport=transport, base_url="http://localhost:8800", headers=headers
            )
            stack.push_async_callback(browser.aclose)
            return browser

        try:
            yield new_browser
        finally:
            await app.state.http_client.aclose()


def _run(
    root: Path,
    upstream: FakeMetabase,
    scenario: Callable[[NewBrowser, AuthProject], Awaitable[None]],
    *,
    auth_on: bool = False,
) -> None:
    async def main() -> None:
        async with _dango(root, upstream, auth_on=auth_on) as new_browser:
            await scenario(new_browser, AuthProject(root))

    asyncio.run(main())


def _session_cookies(resp: httpx.Response) -> list[str]:
    return [c for c in resp.headers.get_list("set-cookie") if c.startswith("metabase.SESSION=")]


@pytest.mark.unit
def test_auth_off_manual_login_keeps_the_session_in_that_browser(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(new_browser: NewBrowser, _project: AuthProject) -> None:
        browser = new_browser()
        login = await browser.post("/metabase/api/session", **_LOGIN)
        assert login.status_code == 200
        (session_cookie,) = _session_cookies(login)
        assert session_cookie.startswith("metabase.SESSION=sess-1;")
        assert "HttpOnly" in session_cookie and "Path=/" in session_cookie
        assert "Domain" not in session_cookie
        names = [c.split("=")[0] for c in login.headers.get_list("set-cookie")]
        assert sorted(names) == ["metabase.DEVICE", "metabase.SESSION", "metabase.TIMEOUT"]

        resp = await browser.get("/metabase/api/user?who=a")
        assert resp.status_code == 200
        assert resp.json() == {"session": "sess-1"}

    _run(tmp_path, upstream, scenario)

    (seen,) = upstream.requests_for("a")
    assert seen.cookies == ["metabase.SESSION=sess-1"]


@pytest.mark.unit
def test_auth_off_a_second_browser_never_gets_the_session(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(new_browser: NewBrowser, _project: AuthProject) -> None:
        await new_browser().post("/metabase/api/session", **_LOGIN)
        other = await new_browser().get("/metabase/api/user?who=b")
        assert other.status_code == 401

    _run(tmp_path, upstream, scenario)

    (seen,) = upstream.requests_for("b")
    assert seen.cookies == []


@pytest.mark.unit
def test_auth_off_logout_clears_the_browser_cookie(tmp_path: Path, upstream: FakeMetabase) -> None:
    async def scenario(new_browser: NewBrowser, _project: AuthProject) -> None:
        browser = new_browser()
        await browser.post("/metabase/api/session", **_LOGIN)
        logout = await browser.delete("/metabase/api/session?who=a")
        assert logout.status_code == 204
        (cleared,) = _session_cookies(logout)
        assert "Max-Age=0" in cleared
        assert (await browser.get("/metabase/api/user?who=a2")).status_code == 401

    _run(tmp_path, upstream, scenario)

    assert upstream.requests_for("a")[0].cookies == ["metabase.SESSION=sess-1"]
    assert upstream.requests_for("a2")[0].cookies == []


@pytest.mark.unit
def test_auth_off_but_not_a_local_request_never_hands_out_the_session(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    async def scenario(new_browser: NewBrowser, _project: AuthProject) -> None:
        browser = new_browser(peer=_REMOTE)
        login = await browser.post("/metabase/api/session", **_LOGIN)
        assert login.status_code == 200
        assert _session_cookies(login) == []
        assert (await browser.get("/metabase/api/user?who=a")).status_code == 401

    _run(tmp_path, upstream, scenario)

    assert upstream.requests_for("a")[0].cookies == []


@pytest.mark.unit
def test_auth_on_manual_login_cookie_is_not_handed_to_the_browser(
    tmp_path: Path, upstream: FakeMetabase
) -> None:
    """A Dango user must never get a Metabase session minted outside the bridge and role checks."""

    async def scenario(new_browser: NewBrowser, project: AuthProject) -> None:
        user_b = project.add_user("b@example.com")
        user_c = project.add_user("c@example.com")
        browser_b = new_browser(cookie=project.session_cookie(user_b))
        browser_c = new_browser(cookie=project.session_cookie(user_c))
        with patch(_REBRIDGE, return_value=None):
            login = await browser_b.post("/metabase/api/session", **_LOGIN)
            assert login.status_code == 200
            assert _session_cookies(login) == []
            assert (await browser_b.get("/metabase/api/user?who=b")).status_code == 401
            assert (await browser_c.get("/metabase/api/user?who=c")).status_code == 401

    _run(tmp_path, upstream, scenario, auth_on=True)

    assert upstream.requests_for("b")[0].cookies == []
    assert upstream.requests_for("c")[0].cookies == []
