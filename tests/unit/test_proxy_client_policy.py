"""tests/unit/test_proxy_client_policy.py

The cookie policy and client factory that keep long-lived proxy HTTP clients stateless, and a guard
that every ``app.state.http_client`` assignment in ``dango/`` goes through the factory.
"""

from __future__ import annotations

import ast
import asyncio
import urllib.request
from http.cookiejar import Cookie
from pathlib import Path

import httpx
import pytest

import dango


def _cookie() -> Cookie:
    return Cookie(
        version=0,
        name="metabase.SESSION",
        value="secret",
        port=None,
        port_specified=False,
        domain="127.0.0.1",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )


_REQUEST = urllib.request.Request("http://127.0.0.1/")


@pytest.mark.unit
def test_policy_refuses_to_store_a_cookie() -> None:
    from dango.web.proxy_client import NoCookiesPolicy

    assert NoCookiesPolicy().set_ok(_cookie(), _REQUEST) is False


@pytest.mark.unit
def test_policy_refuses_to_return_a_cookie() -> None:
    from dango.web.proxy_client import NoCookiesPolicy

    policy = NoCookiesPolicy()
    assert policy.return_ok(_cookie(), _REQUEST) is False
    assert policy.domain_return_ok("127.0.0.1", _REQUEST) is False
    assert policy.path_return_ok("/", _REQUEST) is False


def _client_seeing(seen: list[httpx.Request]) -> httpx.AsyncClient:
    from dango.web.proxy_client import make_proxy_client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers=[("set-cookie", "metabase.SESSION=s; Path=/"), ("set-cookie", "d=1")]
        )

    return make_proxy_client(transport=httpx.MockTransport(handler))


@pytest.mark.unit
def test_factory_client_jar_stays_empty_and_nothing_is_replayed() -> None:
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with _client_seeing(seen) as client:
            await client.get("http://mb.test/api/session")
            assert list(client.cookies.jar) == []
            await client.get("http://mb.test/api/user")

    asyncio.run(scenario())
    assert len(seen) == 2
    assert "cookie" not in seen[1].headers


@pytest.mark.unit
def test_factory_client_never_returns_a_cookie_set_by_hand() -> None:
    seen: list[httpx.Request] = []

    async def scenario() -> None:
        async with _client_seeing(seen) as client:
            client.cookies.set("metabase.SESSION", "planted", domain="mb.test")
            await client.get("http://mb.test/api/user")

    asyncio.run(scenario())
    assert "cookie" not in seen[0].headers


@pytest.mark.unit
def test_factory_client_keeps_the_proxy_defaults() -> None:
    from dango.web.proxy_client import make_proxy_client

    async def scenario() -> None:
        async with make_proxy_client() as client:
            assert client.follow_redirects is False
            assert client.timeout == httpx.Timeout(30.0)

    asyncio.run(scenario())


def _cookie_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers=[("set-cookie", "a=b; Path=/")],
        request=httpx.Request("GET", "http://h.test/"),
    )


def _http_client_assignments() -> list[tuple[str, int, ast.expr]]:
    root = Path(dango.__file__).parent
    found: list[tuple[str, int, ast.expr]] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = node.targets if isinstance(node, ast.Assign) else []
            if any(isinstance(t, ast.Attribute) and t.attr == "http_client" for t in targets):
                assert isinstance(node, ast.Assign)
                found.append((str(path.relative_to(root)), node.lineno, node.value))
    return found


@pytest.mark.unit
def test_every_proxy_client_is_built_by_the_factory() -> None:
    sites = _http_client_assignments()
    assert sites, "the server's lifespan no longer assigns app.state.http_client"
    for where, line, value in sites:
        assert isinstance(value, ast.Call), f"{where}:{line}"
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        assert name == "make_proxy_client", f"{where}:{line} builds a client without the factory"


@pytest.mark.unit
def test_the_health_check_client_is_stateless_too() -> None:
    from dango.web import helpers

    try:
        client = helpers._get_health_check_client()
        client.cookies.extract_cookies(_cookie_response())
        assert list(client.cookies.jar) == []
    finally:
        helpers.close_health_check_client()
