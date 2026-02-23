"""tests/unit/test_digitalocean_client.py

Unit tests for DigitalOceanClient (dango/platform/cloud/digitalocean.py).

All HTTP calls are mocked via unittest.mock — no real network traffic.
``time.sleep`` is patched to avoid slow tests in retry scenarios.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from dango.exceptions import CloudAPIError, CloudAuthError, CloudError
from dango.platform.cloud.digitalocean import DigitalOceanClient

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics an httpx.Response."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.is_success = 200 <= status_code < 300
    mock.headers = headers or {}
    mock.text = ""
    if json_data is not None:
        mock.json.return_value = json_data
        import json

        mock.text = json.dumps(json_data)
    return mock


def _make_client_mock(response: MagicMock) -> MagicMock:
    """Return a mock httpx.Client context manager that returns *response*."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.return_value = response
    return mock_client


# ---------------------------------------------------------------------------
# 1. Initialisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDigitalOceanClientInit:
    def test_token_from_argument(self):
        """Token passed directly is accepted."""
        client = DigitalOceanClient(token="test-token")
        assert client._token == "test-token"

    def test_token_from_env(self, monkeypatch):
        """Token is read from DIGITALOCEAN_TOKEN when not passed."""
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "env-token")
        client = DigitalOceanClient()
        assert client._token == "env-token"

    def test_missing_token_raises(self, monkeypatch):
        """CloudAuthError raised when no token is available."""
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        with pytest.raises(CloudAuthError):
            DigitalOceanClient()

    def test_custom_timeout_and_retries(self):
        """Custom timeout and max_retries are stored."""
        client = DigitalOceanClient(token="t", timeout=60.0, max_retries=5)
        assert client._timeout == 60.0
        assert client._max_retries == 5

    def test_default_timeout_and_retries(self):
        """Default timeout is 30s and max_retries is 3."""
        client = DigitalOceanClient(token="t")
        assert client._timeout == 30.0
        assert client._max_retries == 3


# ---------------------------------------------------------------------------
# 2. Retry / backoff logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetryBackoff:
    def _make_client(self, max_retries: int = 3) -> DigitalOceanClient:
        return DigitalOceanClient(token="test-token", max_retries=max_retries)

    def test_success_no_retry(self):
        """Successful response returns immediately without sleeping."""
        client = self._make_client()
        mock_resp = _mock_response(200, {"droplets": []})
        mock_http = _make_client_mock(mock_resp)

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is mock_resp
        mock_sleep.assert_not_called()

    def test_429_respects_retry_after(self):
        """429 with Retry-After header sleeps for the specified duration."""
        client = self._make_client(max_retries=1)
        rate_limited = _mock_response(429, headers={"Retry-After": "5"})
        rate_limited.is_success = False
        success = _mock_response(200, {"droplets": []})

        mock_http = _make_client_mock(rate_limited)
        mock_http.request.side_effect = [rate_limited, success]

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is success
        mock_sleep.assert_called_once_with(5.0)

    def test_429_without_retry_after_uses_backoff(self):
        """429 without Retry-After header falls back to exponential backoff."""
        client = self._make_client(max_retries=1)
        rate_limited = _mock_response(429)
        rate_limited.is_success = False
        success = _mock_response(200, {"droplets": []})

        mock_http = _make_client_mock(rate_limited)
        mock_http.request.side_effect = [rate_limited, success]

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is success
        mock_sleep.assert_called_once_with(1.0)  # initial delay

    def test_429_with_invalid_retry_after_falls_back_to_backoff(self):
        """Non-numeric Retry-After header (e.g. HTTP-date) falls back to backoff delay."""
        client = self._make_client(max_retries=1)
        rate_limited = _mock_response(429, headers={"Retry-After": "Fri, 31 Dec 1999 23:59:59 GMT"})
        rate_limited.is_success = False
        success = _mock_response(200, {"droplets": []})

        mock_http = _make_client_mock(rate_limited)
        mock_http.request.side_effect = [rate_limited, success]

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is success
        mock_sleep.assert_called_once_with(1.0)  # falls back to initial backoff delay

    def test_500_retry_then_success(self):
        """500 server error retries and succeeds on next attempt."""
        client = self._make_client(max_retries=2)
        server_err = _mock_response(500)
        server_err.is_success = False
        success = _mock_response(200, {"droplets": []})

        mock_http = _make_client_mock(server_err)
        mock_http.request.side_effect = [server_err, success]

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep"),
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is success

    def test_500_all_retries_exhausted_raises_api_error(self):
        """CloudAPIError raised when all retries are exhausted on 500."""
        client = self._make_client(max_retries=2)
        server_err = _mock_response(500)
        server_err.is_success = False

        mock_http = _make_client_mock(server_err)
        mock_http.request.return_value = server_err

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep"),
            pytest.raises(CloudAPIError) as exc_info,
        ):
            client._request_with_retry("GET", "/droplets")

        assert exc_info.value.status_code == 500

    def test_connection_error_retries(self):
        """ConnectError triggers retry then succeeds."""
        client = self._make_client(max_retries=2)
        success = _mock_response(200, {"droplets": []})

        mock_http = _make_client_mock(success)
        mock_http.request.side_effect = [httpx.ConnectError("refused"), success]

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep"),
        ):
            result = client._request_with_retry("GET", "/droplets")

        assert result is success

    def test_connection_error_exhausted_raises_cloud_error(self):
        """CloudError raised when network errors exhaust all retries."""
        client = self._make_client(max_retries=1)

        mock_http = _make_client_mock(MagicMock())
        mock_http.request.side_effect = httpx.ConnectError("refused")

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep"),
            pytest.raises(CloudError),
        ):
            client._request_with_retry("GET", "/droplets")

    def test_401_raises_auth_error_immediately(self):
        """CloudAuthError raised on 401 without retrying."""
        client = self._make_client(max_retries=3)
        auth_err = _mock_response(401)
        auth_err.is_success = False

        mock_http = _make_client_mock(auth_err)

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
            pytest.raises(CloudAuthError),
        ):
            client._request_with_retry("GET", "/droplets")

        # Exactly one HTTP call — no retries
        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()

    def test_404_raises_api_error_immediately(self):
        """CloudAPIError raised on 404 without retrying."""
        client = self._make_client(max_retries=3)
        not_found = _mock_response(404)
        not_found.is_success = False

        mock_http = _make_client_mock(not_found)

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep") as mock_sleep,
            pytest.raises(CloudAPIError) as exc_info,
        ):
            client._request_with_retry("GET", "/droplets/99999")

        assert exc_info.value.status_code == 404
        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Droplet operations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDropletOperations:
    def setup_method(self):
        self.client = DigitalOceanClient(token="test-token")

    def _patch(self, response: MagicMock):
        mock_http = _make_client_mock(response)
        return patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http)

    def test_create_droplet(self):
        """create_droplet returns the droplet dict."""
        droplet = {"id": 1, "name": "test-droplet", "status": "new"}
        resp = _mock_response(202, {"droplet": droplet})

        with self._patch(resp):
            result = self.client.create_droplet(
                name="test-droplet",
                region="nyc1",
                size="s-2vcpu-4gb",
                image="ubuntu-22-04-x64",
                ssh_key_ids=[12345],
                tags=["dango"],
                user_data="#!/bin/bash\necho hi",
            )

        assert result == droplet

    def test_create_droplet_minimal(self):
        """create_droplet works without optional fields."""
        droplet = {"id": 2, "name": "min-droplet"}
        resp = _mock_response(202, {"droplet": droplet})

        with self._patch(resp):
            result = self.client.create_droplet(
                name="min-droplet",
                region="nyc1",
                size="s-2vcpu-4gb",
                image="ubuntu-22-04-x64",
            )

        assert result["id"] == 2

    def test_get_droplet(self):
        """get_droplet returns the droplet dict for the given ID."""
        droplet = {"id": 42, "name": "my-droplet"}
        resp = _mock_response(200, {"droplet": droplet})

        with self._patch(resp):
            result = self.client.get_droplet(42)

        assert result == droplet

    def test_list_droplets_single_page(self):
        """list_droplets returns all droplets from a single page."""
        droplets = [{"id": 1}, {"id": 2}]
        resp = _mock_response(200, {"droplets": droplets, "links": {}})

        with self._patch(resp):
            result = self.client.list_droplets()

        assert result == droplets

    def test_list_droplets_with_tag(self):
        """list_droplets passes tag_name as a query param."""
        droplets = [{"id": 3, "tags": ["dango"]}]
        resp = _mock_response(200, {"droplets": droplets, "links": {}})
        mock_http = _make_client_mock(resp)

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            result = self.client.list_droplets(tag_name="dango")

        assert result == droplets
        call_params = mock_http.request.call_args[1].get("params") or {}
        assert call_params.get("tag_name") == "dango"

    def test_list_droplets_pagination(self):
        """list_droplets collects all pages when pagination links are present."""
        page1 = {
            "droplets": [{"id": 1}],
            "links": {
                "pages": {"next": "https://api.digitalocean.com/v2/droplets?page=2&per_page=200"}
            },
        }
        page2 = {"droplets": [{"id": 2}], "links": {}}
        resp1 = _mock_response(200, page1)
        resp2 = _mock_response(200, page2)
        mock_http = _make_client_mock(resp1)
        mock_http.request.side_effect = [resp1, resp2]

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            result = self.client.list_droplets()

        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2

    def test_delete_droplet(self):
        """delete_droplet succeeds on 204 response."""
        resp = _mock_response(204)
        with self._patch(resp):
            self.client.delete_droplet(42)  # should not raise

    def test_power_off(self):
        """power_off returns the action dict."""
        action = {"id": 9, "type": "power_off", "status": "in-progress"}
        resp = _mock_response(201, {"action": action})

        with self._patch(resp):
            result = self.client.power_off(42)

        assert result == action

    def test_power_on(self):
        """power_on returns the action dict."""
        action = {"id": 10, "type": "power_on", "status": "in-progress"}
        resp = _mock_response(201, {"action": action})

        with self._patch(resp):
            result = self.client.power_on(42)

        assert result == action

    def test_resize(self):
        """resize passes the size parameter and returns action dict."""
        action = {"id": 11, "type": "resize", "status": "in-progress"}
        resp = _mock_response(201, {"action": action})
        mock_http = _make_client_mock(resp)

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            result = self.client.resize(42, "s-4vcpu-8gb")

        assert result == action
        payload = mock_http.request.call_args[1].get("json") or {}
        assert payload["size"] == "s-4vcpu-8gb"


# ---------------------------------------------------------------------------
# 4. SSH Key operations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSSHKeyOperations:
    def setup_method(self):
        self.client = DigitalOceanClient(token="test-token")

    def _patch(self, response: MagicMock):
        mock_http = _make_client_mock(response)
        return patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http)

    def test_upload_ssh_key(self):
        """upload_ssh_key returns the ssh_key dict."""
        key = {"id": 100, "name": "my-key", "fingerprint": "aa:bb"}
        resp = _mock_response(201, {"ssh_key": key})

        with self._patch(resp):
            result = self.client.upload_ssh_key("my-key", "ssh-rsa AAAA...")

        assert result == key

    def test_list_ssh_keys(self):
        """list_ssh_keys returns all keys from a single page."""
        keys = [{"id": 1, "name": "key1"}, {"id": 2, "name": "key2"}]
        resp = _mock_response(200, {"ssh_keys": keys, "links": {}})

        with self._patch(resp):
            result = self.client.list_ssh_keys()

        assert result == keys

    def test_delete_ssh_key_by_id(self):
        """delete_ssh_key succeeds on 204 response (integer ID)."""
        resp = _mock_response(204)
        with self._patch(resp):
            self.client.delete_ssh_key(100)  # should not raise

    def test_delete_ssh_key_by_fingerprint(self):
        """delete_ssh_key accepts a fingerprint string."""
        resp = _mock_response(204)
        mock_http = _make_client_mock(resp)

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            self.client.delete_ssh_key("aa:bb:cc")

        url = mock_http.request.call_args[0][1]
        assert "aa:bb:cc" in url


# ---------------------------------------------------------------------------
# 5. Firewall operations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirewallOperations:
    def setup_method(self):
        self.client = DigitalOceanClient(token="test-token")

    def _patch(self, response: MagicMock):
        mock_http = _make_client_mock(response)
        return patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http)

    def test_create_firewall(self):
        """create_firewall returns the firewall dict."""
        firewall = {"id": "fw-123", "name": "dango-fw"}
        resp = _mock_response(202, {"firewall": firewall})

        with self._patch(resp):
            result = self.client.create_firewall(
                name="dango-fw",
                inbound_rules=[
                    {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}
                ],
                outbound_rules=[
                    {
                        "protocol": "tcp",
                        "ports": "all",
                        "destinations": {"addresses": ["0.0.0.0/0"]},
                    }
                ],
                droplet_ids=[42],
            )

        assert result == firewall

    def test_add_droplets_to_firewall(self):
        """add_droplets_to_firewall sends correct payload."""
        resp = _mock_response(204)
        mock_http = _make_client_mock(resp)

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            self.client.add_droplets_to_firewall("fw-123", [42, 43])

        payload = mock_http.request.call_args[1].get("json") or {}
        assert payload["droplet_ids"] == [42, 43]

    def test_delete_firewall(self):
        """delete_firewall succeeds on 204 response."""
        resp = _mock_response(204)
        with self._patch(resp):
            self.client.delete_firewall("fw-123")  # should not raise

    def test_get_firewall(self):
        """get_firewall returns the firewall dict for the given ID."""
        firewall = {"id": "fw-abc", "name": "dango-fw-42"}
        resp = _mock_response(200, {"firewall": firewall})

        with self._patch(resp):
            result = self.client.get_firewall("fw-abc")

        assert result == firewall

    def test_update_firewall(self):
        """update_firewall sends PUT with all fields and returns the firewall dict."""
        firewall = {"id": "fw-abc", "name": "dango-fw-42"}
        resp = _mock_response(200, {"firewall": firewall})
        mock_http = _make_client_mock(resp)

        inbound = [{"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}]
        outbound = [
            {"protocol": "tcp", "ports": "all", "destinations": {"addresses": ["0.0.0.0/0"]}}
        ]

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            result = self.client.update_firewall(
                firewall_id="fw-abc",
                name="dango-fw-42",
                inbound_rules=inbound,
                outbound_rules=outbound,
                droplet_ids=[42],
            )

        assert result == firewall
        # Verify PUT method was used
        method = mock_http.request.call_args[0][0]
        assert method == "PUT"
        payload = mock_http.request.call_args[1].get("json") or {}
        assert payload["name"] == "dango-fw-42"
        assert payload["inbound_rules"] == inbound
        assert payload["outbound_rules"] == outbound
        assert payload["droplet_ids"] == [42]

    def test_update_firewall_with_empty_droplet_ids(self):
        """update_firewall passes empty droplet_ids list (not omitted)."""
        firewall = {"id": "fw-abc"}
        resp = _mock_response(200, {"firewall": firewall})
        mock_http = _make_client_mock(resp)

        with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http):
            self.client.update_firewall(
                firewall_id="fw-abc",
                name="dango-fw-42",
                inbound_rules=[],
                outbound_rules=[],
                droplet_ids=[],  # empty list — should be included, not omitted
            )

        payload = mock_http.request.call_args[1].get("json") or {}
        assert "droplet_ids" in payload
        assert payload["droplet_ids"] == []


# ---------------------------------------------------------------------------
# 6. Error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorHandling:
    def setup_method(self):
        self.client = DigitalOceanClient(token="test-token")

    def test_api_error_stores_status_and_body(self):
        """CloudAPIError carries status_code and response_body."""
        body = '{"id": "not_found", "message": "The resource you requested could not be found"}'
        resp = _mock_response(404)
        resp.text = body
        resp.is_success = False

        mock_http = _make_client_mock(resp)

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            pytest.raises(CloudAPIError) as exc_info,
        ):
            self.client._request_with_retry("GET", "/droplets/99")

        exc = exc_info.value
        assert exc.status_code == 404
        assert exc.response_body == body

    def test_non_json_error_body_handled(self):
        """CloudAPIError raised even when the response body is not valid JSON."""
        resp = _mock_response(503)
        resp.text = "Service Unavailable"
        resp.is_success = False
        resp.json.side_effect = Exception("not json")

        mock_http = _make_client_mock(resp)
        mock_http.request.return_value = resp

        with (
            patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=mock_http),
            patch("dango.platform.cloud.digitalocean.time.sleep"),
            pytest.raises(CloudAPIError),
        ):
            self.client._request_with_retry("GET", "/droplets")
