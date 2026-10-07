"""tests/unit/test_size_availability.py

Unit tests for dango/platform/cloud/size_availability.py and
DigitalOceanClient.list_sizes, using a recorded /v2/sizes response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudAuthError, CloudError
from dango.platform.cloud.digitalocean import DigitalOceanClient
from dango.platform.cloud.size_availability import (
    SizeUnavailableError,
    check_size_in_region,
    fetch_sizes,
    offerable_regions,
    regions_for_size,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "do_sizes_s_2vcpu_4gb.json"
EXPECTED = {"ams3", "blr1", "fra1", "lon1", "sfo2", "sgp1", "syd1", "tor1"}


@pytest.fixture()
def payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


@pytest.mark.unit
class TestRegionsForSize:
    def test_recorded_response(self, payload):
        """Recorded live data: s-2vcpu-4gb is offered in 8 regions, not nyc1."""
        assert regions_for_size(payload["sizes"], "s-2vcpu-4gb") == EXPECTED
        assert "nyc1" not in regions_for_size(payload, "s-2vcpu-4gb")

    def test_unknown_slug_is_empty(self, payload):
        assert regions_for_size(payload["sizes"], "s-99vcpu-1tb") == set()

    def test_unavailable_flag_is_empty(self, payload):
        payload["sizes"][0]["available"] = False
        assert regions_for_size(payload["sizes"], "s-2vcpu-4gb") == set()


@pytest.mark.unit
class TestOfferableRegions:
    def test_unknown_api_regions_skipped(self):
        """Matrix 6: API slug missing from the static list is not offered."""
        slugs = [r.slug for r in offerable_regions({"ams3", "sgp1", "zzz9"})]
        assert slugs == ["ams3", "sgp1"]


@pytest.mark.unit
class TestFetchAndCheck:
    def test_fetch_sizes_failure_returns_none(self):
        client = MagicMock()
        client.list_sizes.side_effect = CloudError("boom")
        assert fetch_sizes(client) is None

    def test_fetch_sizes_missing_token_returns_none(self, monkeypatch):
        """Matrix 3: no token -> None, no exception."""
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        monkeypatch.setattr("dango.config.cloud_credentials.get_do_token", lambda *a, **k: None)
        assert fetch_sizes() is None

    def test_available_returns_true(self, payload):
        client = MagicMock()
        client.list_sizes.return_value = payload["sizes"]
        assert check_size_in_region("sgp1", "s-2vcpu-4gb", client=client) is True

    def test_unavailable_lists_regions(self, payload):
        client = MagicMock()
        client.list_sizes.return_value = payload["sizes"]
        with pytest.raises(SizeUnavailableError) as exc:
            check_size_in_region("nyc1", "s-2vcpu-4gb", client=client)
        msg = str(exc.value)
        assert "nyc1" in msg
        assert all(r in msg for r in EXPECTED)

    def test_unknown_size_raises(self, payload):
        client = MagicMock()
        client.list_sizes.return_value = payload["sizes"]
        with pytest.raises(SizeUnavailableError):
            check_size_in_region("nyc1", "s-99vcpu-1tb", client=client)

    def test_check_failure_returns_false(self):
        client = MagicMock()
        client.list_sizes.side_effect = CloudAuthError("bad token")
        assert check_size_in_region("nyc1", "s-2vcpu-4gb", client=client) is False


@pytest.mark.unit
class TestListSizes:
    def test_list_sizes_paginates(self, payload, monkeypatch):
        client = DigitalOceanClient(token="fake-token", max_retries=0)
        page1 = MagicMock()
        page1.json.return_value = {
            "sizes": payload["sizes"],
            "links": {"pages": {"next": "https://api.digitalocean.com/v2/sizes?page=2"}},
        }
        page2 = MagicMock()
        page2.json.return_value = {"sizes": [{"slug": "s-4vcpu-8gb", "regions": ["nyc1"]}]}
        calls: list[tuple[str, str]] = []
        params_seen: list[Any] = []

        def fake(method, path, **kwargs):
            calls.append((method, path))
            params_seen.append(kwargs.get("params"))
            return page1 if len(calls) == 1 else page2

        monkeypatch.setattr(client, "_request_with_retry", fake)
        sizes = client.list_sizes()
        assert [s["slug"] for s in sizes] == ["s-2vcpu-4gb", "s-4vcpu-8gb"]
        assert calls == [("GET", "/sizes"), ("GET", "/sizes?page=2")]
        assert params_seen == [{"per_page": 200}, {}]


@pytest.mark.unit
class TestProbeClient:
    def test_api_failure_is_one_request_and_no_sleep(self, monkeypatch):
        """The default probe never retries: one request, no backoff sleep."""
        import httpx

        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "fake-token")
        requests: list[Any] = []

        def fake_request(self, method, path, **kwargs):
            requests.append(path)
            raise httpx.ConnectError("down")

        monkeypatch.setattr(DigitalOceanClient, "_request", fake_request)
        sleep = MagicMock()
        monkeypatch.setattr("dango.platform.cloud.digitalocean.time.sleep", sleep)
        assert check_size_in_region("nyc1", "s-2vcpu-4gb") is False
        assert requests == ["/sizes"]
        sleep.assert_not_called()

    def test_probe_client_settings(self, monkeypatch):
        seen: dict[str, Any] = {}

        class Spy(DigitalOceanClient):
            def __init__(self, *a: Any, **k: Any) -> None:
                seen.update(k)
                super().__init__(token="fake", **k)

            def list_sizes(self) -> list[dict[str, Any]]:
                return []

        monkeypatch.setattr("dango.platform.cloud.digitalocean.DigitalOceanClient", Spy)
        fetch_sizes()
        assert seen == {"timeout": 10.0, "max_retries": 0}
