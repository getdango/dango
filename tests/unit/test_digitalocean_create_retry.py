"""tests/unit/test_digitalocean_create_retry.py

Create calls (droplet, firewall, SSH key) are never blindly repeated: an accepted request whose answer
is lost or a 5xx is reconciled by name instead. Uses a stateful fake DigitalOcean over httpx.MockTransport.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from dango.exceptions import CloudAPIError, CloudError
from dango.platform.cloud.digitalocean import DigitalOceanClient
from dango.platform.cloud.firewall import create_default_firewall
from dango.platform.cloud.provisioning import provision_droplet

_REAL_HTTPX_CLIENT = httpx.Client
_PUBLIC_KEY = "ssh-ed25519 AAAATESTKEY dango"
_DROPLET_BODY: dict[str, Any] = {
    "name": "dango-proj",
    "region": "sgp1",
    "size": "s-2vcpu-4gb",
    "image": "ubuntu-22-04-x64",
    "tags": ["dango"],
}


class FakeDigitalOcean:
    """Minimal stateful DigitalOcean API.

    ``script`` maps ``"POST /droplets"`` (etc.) to a list of outcomes consumed one per POST:
    ``"ok"``, ``("accepted", 502)`` (stored, answer replaced by an HTTP status),
    ``"accepted_timeout"`` (stored, answer lost), ``("rejected", 502)`` (not stored),
    ``"connect_error"`` (request never sent) or ``429``.
    """

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = script or {}
        self.droplets: list[dict[str, Any]] = []
        self.firewalls: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self._next_id = 1000

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.requests if (m, p) == (method, path))

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _store(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/droplets":
            item = {
                **body,
                "id": self._new_id(),
                "status": "active",
                "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.7"}]},
            }
            self.droplets.append(item)
        elif path == "/firewalls":
            item = {**body, "id": f"fw-{self._new_id()}"}
            self.firewalls.append(item)
        else:
            item = {**body, "id": self._new_id(), "fingerprint": "aa:bb"}
            self.keys.append(item)
        return item

    def _post(self, request: httpx.Request, path: str) -> httpx.Response:
        import json

        keys = {"/droplets": "droplet", "/firewalls": "firewall", "/account/keys": "ssh_key"}
        outcomes = self.script.get(f"POST {path}", [])
        outcome = outcomes.pop(0) if outcomes else "ok"
        if outcome == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if outcome == 429:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "slow"})
        if isinstance(outcome, tuple) and outcome[0] == "rejected":
            return httpx.Response(outcome[1], json={"message": "boom"})
        item = self._store(path, json.loads(request.content))
        if outcome == "accepted_timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        if isinstance(outcome, tuple) and outcome[0] == "accepted":
            return httpx.Response(outcome[1], json={"message": "upstream error"})
        return httpx.Response(202, json={keys[path]: item})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        self.requests.append((request.method, path))
        if request.method == "POST":
            return self._post(request, path)
        if request.method == "GET":
            if path == "/droplets":
                tag = request.url.params.get("tag_name")
                found = [d for d in self.droplets if not tag or tag in d.get("tags", [])]
                return httpx.Response(200, json={"droplets": found})
            if path.startswith("/droplets/"):
                wanted = int(path.rsplit("/", 1)[1])
                found = [d for d in self.droplets if d["id"] == wanted]
                return httpx.Response(200, json={"droplet": found[0]})
            if path == "/firewalls":
                return httpx.Response(200, json={"firewalls": self.firewalls})
            if path == "/account/keys":
                return httpx.Response(200, json={"ssh_keys": self.keys})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def fake_do(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Route DigitalOceanClient traffic to a FakeDigitalOcean; no real sleeping."""
    holder: dict[str, FakeDigitalOcean] = {}

    def install(script: dict[str, list[Any]] | None = None) -> FakeDigitalOcean:
        server = FakeDigitalOcean(script)
        holder["server"] = server

        def factory(*_args: Any, **kwargs: Any) -> httpx.Client:
            return _REAL_HTTPX_CLIENT(
                transport=httpx.MockTransport(server.handle), timeout=kwargs.get("timeout")
            )

        monkeypatch.setattr("dango.platform.cloud.digitalocean.httpx.Client", factory)
        return server

    monkeypatch.setattr("dango.platform.cloud.digitalocean.time.sleep", lambda _s: None)
    return install


def _client() -> DigitalOceanClient:
    return DigitalOceanClient(token="test-token")


@pytest.mark.unit
class TestDropletCreateIsNotRepeated:
    def test_accepted_then_502_creates_exactly_one_droplet_and_adopts_it(self, fake_do):
        """The live sequence: request accepted, answer is a 502; one POST, one droplet."""
        server = fake_do({"POST /droplets": [("accepted", 502)]})

        droplet = _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 1
        assert len(server.droplets) == 1
        assert droplet["id"] == server.droplets[0]["id"]

    def test_accepted_then_read_timeout_adopts_the_droplet(self, fake_do):
        server = fake_do({"POST /droplets": ["accepted_timeout"]})

        droplet = _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 1
        assert len(server.droplets) == 1
        assert droplet["id"] == server.droplets[0]["id"]

    def test_ambiguous_failure_with_nothing_found_raises_and_does_not_retry(self, fake_do):
        server = fake_do({"POST /droplets": [("rejected", 502), "ok"]})

        with pytest.raises(CloudError, match="could not be confirmed"):
            _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 1
        assert server.droplets == []

    def test_several_matches_raises_listing_every_id(self, fake_do):
        server = fake_do({"POST /droplets": [("accepted", 502)]})
        for existing_id in (11, 12):
            server.droplets.append({**_DROPLET_BODY, "id": existing_id, "status": "active"})

        with pytest.raises(CloudError) as exc_info:
            _client().create_droplet(**_DROPLET_BODY)

        message = str(exc_info.value)
        assert "11" in message and "12" in message
        assert server.count("POST", "/droplets") == 1

    def test_429_is_retried_because_the_request_did_not_take_effect(self, fake_do):
        server = fake_do({"POST /droplets": [429]})

        droplet = _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 2
        assert len(server.droplets) == 1
        assert droplet["id"] == server.droplets[0]["id"]

    def test_connect_error_is_retried_because_nothing_was_sent(self, fake_do):
        server = fake_do({"POST /droplets": ["connect_error"]})

        droplet = _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 2
        assert len(server.droplets) == 1
        assert droplet["id"] == server.droplets[0]["id"]

    def test_client_error_still_raises_api_error_without_lookup(self, fake_do):
        server = fake_do({"POST /droplets": [("rejected", 422)]})

        with pytest.raises(CloudAPIError):
            _client().create_droplet(**_DROPLET_BODY)

        assert server.count("POST", "/droplets") == 1
        assert server.count("GET", "/droplets") == 0


@pytest.mark.unit
class TestOtherCreatesAreNotRepeated:
    def test_firewall_accepted_then_502_adopts_by_name(self, fake_do):
        server = fake_do({"POST /firewalls": [("accepted", 502)]})

        firewall = create_default_firewall(_client(), 4242)

        assert server.count("POST", "/firewalls") == 1
        assert len(server.firewalls) == 1
        assert firewall["id"] == server.firewalls[0]["id"]

    def test_ssh_key_accepted_then_timeout_adopts_by_name(self, fake_do):
        server = fake_do({"POST /account/keys": ["accepted_timeout"]})

        key = _client().upload_ssh_key("dango-proj-1", _PUBLIC_KEY)

        assert server.count("POST", "/account/keys") == 1
        assert len(server.keys) == 1
        assert key["id"] == server.keys[0]["id"]

    def test_ssh_key_nothing_found_raises(self, fake_do):
        server = fake_do({"POST /account/keys": [("rejected", 503)]})

        with pytest.raises(CloudError, match="could not be confirmed"):
            _client().upload_ssh_key("dango-proj-1", _PUBLIC_KEY)

        assert server.count("POST", "/account/keys") == 1


@pytest.mark.unit
class TestReadsAndDeletesKeepRetrying:
    def test_get_is_retried_on_502(self, fake_do):
        server = fake_do()
        server.droplets.append({**_DROPLET_BODY, "id": 5, "status": "active"})
        real_handle = server.handle
        calls = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/droplets/5"):
                calls["n"] += 1
                if calls["n"] == 1:
                    return httpx.Response(502, json={"message": "bad gateway"})
            return real_handle(request)

        with patch.object(server, "handle", flaky):
            droplet = _client().get_droplet(5)

        assert droplet["id"] == 5
        assert calls["n"] == 2

    def test_delete_is_retried_on_503(self, fake_do):
        server = fake_do()
        seen: list[int] = []

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                seen.append(1)
                return httpx.Response(503 if len(seen) == 1 else 204)
            return httpx.Response(404)

        with patch.object(server, "handle", flaky):
            _client().delete_droplet(5)

        assert len(seen) == 2


@pytest.mark.unit
class TestProvisionDropletEndToEnd:
    def test_lost_create_answer_leaves_exactly_one_droplet(self, fake_do):
        """provision_droplet over the fake API: a 502 after acceptance must not cause a second droplet."""
        server = fake_do({"POST /droplets": [("accepted", 502)]})

        with (
            patch("dango.platform.cloud.provisioning.wait_for_ssh"),
            patch("dango.platform.cloud.provisioning.time.sleep"),
        ):
            result = provision_droplet(_client(), "dango-proj", "sgp1", "s-2vcpu-4gb", [7])

        assert server.count("POST", "/droplets") == 1
        assert len(server.droplets) == 1
        assert result["id"] == server.droplets[0]["id"]

    def test_older_droplet_with_the_same_base_name_is_not_mistaken_for_the_new_one(self, fake_do):
        server = fake_do({"POST /droplets": [("accepted", 502)]})
        server.droplets.append(
            {"id": 1, "name": "dango-proj", "status": "active", "tags": ["dango"]}
        )

        with (
            patch("dango.platform.cloud.provisioning.wait_for_ssh"),
            patch("dango.platform.cloud.provisioning.time.sleep"),
        ):
            result = provision_droplet(_client(), "dango-proj", "sgp1", "s-2vcpu-4gb", [7])

        assert server.count("POST", "/droplets") == 1
        assert len(server.droplets) == 2
        assert result["id"] != 1
        assert result["id"] == server.droplets[1]["id"]
