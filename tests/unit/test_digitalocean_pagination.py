"""tests/unit/test_digitalocean_pagination.py

Every paginated DigitalOcean list call must reach page 2. Under httpx 0.28 an empty ``params`` dict
replaces the query string of the next-page URL, so the request that goes out is a bare first page.
These tests drive the real httpx request path through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from dango.platform.cloud import digitalocean as do_module
from dango.platform.cloud.create_reconcile import list_firewalls
from dango.platform.cloud.digitalocean import DigitalOceanClient

_REAL_HTTPX_CLIENT = httpx.Client
_TOTAL = 250
_MAX_REQUESTS = 8

# list path -> (response key, extra fields per item)
_COLLECTIONS: dict[str, str] = {
    "/sizes": "sizes",
    "/droplets": "droplets",
    "/account/keys": "ssh_keys",
    "/firewalls": "firewalls",
}


class PagedDigitalOcean:
    """DigitalOcean-style paginated GETs (page, per_page, absolute links.pages.next)."""

    def __init__(self) -> None:
        self.items: dict[str, list[dict[str, Any]]] = {
            path: [{"id": i, "name": f"item-{i}", "slug": f"s-{i}"} for i in range(_TOTAL)]
            for path in _COLLECTIONS
        }
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        self.seen.append(f"{path}?{request.url.query.decode()}")
        if len(self.seen) > _MAX_REQUESTS:
            return httpx.Response(400, json={"message": "stopped: never reached the last page"})
        if request.method == "POST":
            key = {"/droplets": "droplet", "/firewalls": "firewall", "/account/keys": "ssh_key"}[
                path
            ]
            item = {**json.loads(request.content), "id": 9999}
            self.items[path].append(item)
            return httpx.Response(502, json={"message": "upstream error", key: None})
        key = _COLLECTIONS[path]
        params = request.url.params
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 20))
        body: dict[str, Any] = {
            key: self.items[path][(page - 1) * per_page : page * per_page],
            "links": {},
        }
        if page * per_page < len(self.items[path]):
            body["links"] = {
                "pages": {
                    "next": f"{do_module._BASE_URL}{path}?page={page + 1}&per_page={per_page}"
                }
            }
        return httpx.Response(200, json=body)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> PagedDigitalOcean:
    server = PagedDigitalOcean()

    def factory(*_args: Any, **kwargs: Any) -> httpx.Client:
        return _REAL_HTTPX_CLIENT(
            transport=httpx.MockTransport(server.handle), timeout=kwargs.get("timeout")
        )

    monkeypatch.setattr("dango.platform.cloud.digitalocean.httpx.Client", factory)
    monkeypatch.setattr("dango.platform.cloud.digitalocean.time.sleep", lambda _s: None)
    return server


def _client() -> DigitalOceanClient:
    return DigitalOceanClient(token="test-token")


@pytest.mark.unit
class TestListHelpersReachEveryPage:
    @pytest.mark.parametrize(
        ("path", "call"),
        [
            ("/sizes", lambda c: c.list_sizes()),
            ("/droplets", lambda c: c.list_droplets()),
            ("/account/keys", lambda c: c.list_ssh_keys()),
            ("/firewalls", lambda c: list_firewalls(c)),
        ],
    )
    def test_second_page_request_carries_the_page_query(
        self, paged: PagedDigitalOcean, path: str, call: Callable[[DigitalOceanClient], Any]
    ):
        result = call(_client())

        assert len(result) == _TOTAL
        assert paged.seen == [f"{path}?per_page=200", f"{path}?page=2&per_page=200"]


@pytest.mark.unit
class TestReconciliationLookupReachesEveryPage:
    def test_droplet_created_past_the_first_page_is_adopted(self, paged: PagedDigitalOcean):
        droplet = _client().create_droplet(
            name="dango-proj-abc123", region="sgp1", size="s-2vcpu-4gb", image="ubuntu-22-04-x64"
        )

        assert droplet["id"] == 9999
        assert any("page=2" in s for s in paged.seen)

    def test_ssh_key_created_past_the_first_page_is_adopted(self, paged: PagedDigitalOcean):
        key = _client().upload_ssh_key("dango-proj-1", "ssh-ed25519 AAAATESTKEY dango")

        assert key["id"] == 9999
        assert any("page=2" in s for s in paged.seen)

    def test_firewall_created_past_the_first_page_is_adopted(self, paged: PagedDigitalOcean):
        firewall = _client().create_firewall("dango-fw-1", [], [])

        assert firewall["id"] == 9999
        assert any("page=2" in s for s in paged.seen)
