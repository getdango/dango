"""dango/platform/cloud/digitalocean.py

DigitalOcean REST API v2 client for Dango cloud provisioning.

Provides synchronous HTTPS access to DigitalOcean Droplets, SSH Keys, and
Firewalls. Uses ``httpx`` for HTTP transport with exponential-backoff retries
on transient errors (429, 5xx, network failures).

All methods return raw ``dict`` or ``list[dict]`` values from the DO API
response.  Downstream tasks (TASK-023+) extract the fields they need.

Authentication
--------------
Pass a Personal Access Token via the ``token`` argument, or set the
``DIGITALOCEAN_TOKEN`` environment variable.

Retry policy
------------
- 429 rate-limit: respects ``Retry-After`` header, else falls back to backoff
- 500/502/503/504 server errors: exponential backoff
- Connection / timeout errors: exponential backoff
- Create POSTs (droplet, firewall, SSH key) are repeated only when the request
  provably did not take effect (429, or a connection failure before sending);
  any other failure is reconciled by name instead (``create_reconcile.py``)
- 401: raises ``CloudAuthError`` immediately (no retry)
- 400/403/404/422: raises ``CloudAPIError`` immediately (no retry)
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, cast

import httpx

from dango.exceptions import CloudAPIError, CloudAuthError, CloudError
from dango.platform.cloud.create_reconcile import (
    CreateOutcomeUnknown,
    list_firewalls,
    reconcile_create,
    same_public_key,
)

_BASE_URL = "https://api.digitalocean.com/v2"
_MAX_ERROR_MESSAGE_CHARS = 300


def _format_api_error(status: int, response: httpx.Response) -> str:
    """Build an error string from DO's JSON ``message``/``id``, if present."""
    text = f"DigitalOcean API error: HTTP {status}"
    try:
        data = response.json()
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    message = data.get("message")
    if not isinstance(message, str) or not message:
        return text
    text = f"{text}: {message[:_MAX_ERROR_MESSAGE_CHARS]}"
    error_id = data.get("id")
    if isinstance(error_id, str) and error_id:
        text = f"{text} ({error_id[:_MAX_ERROR_MESSAGE_CHARS]})"
    return text


_NO_RETRY_CODES = {400, 401, 403, 404, 422}
_RETRY_CODES = {429, 500, 502, 503, 504}


class DigitalOceanClient:
    """Synchronous client for the DigitalOcean v2 REST API.

    Args:
        token: Personal Access Token. Reads ``DIGITALOCEAN_TOKEN`` env var
            if not provided.
        timeout: Per-request timeout in seconds. Default: 30.
        max_retries: Maximum number of retry attempts for transient errors.
            Default: 3.

    Raises:
        CloudAuthError: If no token is found from argument or environment.
    """

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        resolved = token or os.environ.get("DIGITALOCEAN_TOKEN")
        # BUG-127: Fall back to stored credential
        if not resolved:
            from dango.config.cloud_credentials import get_do_token

            resolved = get_do_token()
        if not resolved:
            raise CloudAuthError(
                "No DigitalOcean token found. Run 'dango deploy' to configure, "
                "or set the DIGITALOCEAN_TOKEN environment variable.",
            )
        self._token = resolved
        self._timeout = timeout
        self._max_retries = max_retries
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute a single HTTP request and return the raw response."""
        url = f"{_BASE_URL}{path}"
        with httpx.Client(timeout=self._timeout) as client:
            return client.request(
                method,
                url,
                headers=self._headers,
                json=json,
                params=params,
            )

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Map a non-2xx response to an appropriate exception.

        Raises:
            CloudAuthError: For 401 Unauthorized.
            CloudAPIError: For all other error status codes.
        """
        try:
            body = response.text
        except Exception:
            body = ""

        if response.status_code == 401:
            raise CloudAuthError(
                "DigitalOcean API authentication failed. Check your token.",
            )
        raise CloudAPIError(
            _format_api_error(response.status_code, response),
            status_code=response.status_code,
            response_body=body,
        )

    def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        create: bool = False,
    ) -> httpx.Response:
        """Execute a request with exponential-backoff retry on transient errors.

        With ``create=True`` (a POST that creates a resource) only 429 and
        connection failures before the request was sent are retried; any other
        transient failure raises ``CreateOutcomeUnknown`` so the caller can
        look the resource up instead of creating it twice.

        Retries on:
        - HTTP 429 (rate limit) — respects ``Retry-After`` header
        - HTTP 500/502/503/504 (server errors)
        - ``httpx.ConnectError``, ``httpx.TimeoutException``

        Does NOT retry:
        - HTTP 401 → raises ``CloudAuthError``
        - HTTP 400/403/404/422 → raises ``CloudAPIError``

        Returns:
            The successful ``httpx.Response``.

        Raises:
            CloudAuthError: On 401 or exhausted retries after auth failure.
            CloudAPIError: On non-retryable 4xx or exhausted 5xx retries.
            CloudError: On exhausted network-level retries.
        """
        delay = 1.0

        for attempt in range(self._max_retries + 1):
            try:
                response = self._request(method, path, json=json, params=params)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if create and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise CreateOutcomeUnknown(type(exc).__name__) from exc
                if attempt == self._max_retries:
                    raise CloudError(
                        f"Network error after {self._max_retries} retries: {exc}",
                    ) from exc
                time.sleep(delay)
                delay *= 2.0
                continue
            except httpx.TransportError as exc:
                if create:
                    raise CreateOutcomeUnknown(type(exc).__name__) from exc
                raise

            # Non-retryable errors — raise immediately
            if response.status_code in _NO_RETRY_CODES:
                self._handle_error_response(response)

            # Success
            if response.is_success:
                return response

            if create and response.status_code >= 500:
                raise CreateOutcomeUnknown(f"HTTP {response.status_code}")

            # Retryable server errors
            if response.status_code in _RETRY_CODES:
                if attempt == self._max_retries:
                    self._handle_error_response(response)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else delay
                    except (ValueError, TypeError):
                        # Retry-After may be an HTTP-date string; fall back to backoff
                        wait = delay
                else:
                    wait = delay

                time.sleep(wait)
                delay *= 2.0
                continue

            # Any other non-2xx (e.g. 301 redirects not followed)
            self._handle_error_response(response)

        # Should never reach here, but satisfies the type checker
        raise CloudError("Retry loop exhausted without result")  # pragma: no cover

    def _create(
        self,
        path: str,
        payload: dict[str, Any],
        key: str,
        kind: str,
        find: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """POST a create request once; on an unknown outcome adopt the resource by name."""
        try:
            response = self._request_with_retry("POST", path, json=payload, create=True)
            return cast(dict[str, Any], response.json()[key])
        except CreateOutcomeUnknown as exc:
            detail = exc.detail
        except (ValueError, KeyError, TypeError):
            detail = "unreadable response"
        return reconcile_create(kind, str(payload["name"]), detail, find)

    # ------------------------------------------------------------------
    # Size catalogue
    # ------------------------------------------------------------------

    def list_sizes(self) -> list[dict[str, Any]]:
        """List all Droplet sizes with the regions each is offered in.

        Paginates through all pages (``per_page=200``).

        Returns:
            List of ``size`` objects (``slug``, ``regions``, ``available``,
            ``price_monthly``, ...).
        """
        sizes: list[dict[str, Any]] = []
        path: str | None = "/sizes"
        params: dict[str, Any] | None = {"per_page": 200}

        while path:
            response = self._request_with_retry("GET", path, params=params)
            data: dict[str, Any] = response.json()
            sizes.extend(data.get("sizes", []))
            params = None
            path = _next_page_path(data)

        return sizes

    # ------------------------------------------------------------------
    # Droplet operations
    # ------------------------------------------------------------------

    def create_droplet(
        self,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_key_ids: list[int] | None = None,
        tags: list[str] | None = None,
        user_data: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Droplet.

        Args:
            name: Droplet hostname.
            region: DO region slug (e.g. ``"nyc1"``).
            size: Droplet size slug (e.g. ``"s-2vcpu-4gb"``).
            image: Image slug or ID (e.g. ``"ubuntu-22-04-x64"``).
            ssh_key_ids: List of SSH key IDs to install on the Droplet.
            tags: List of tag names to apply to the Droplet.
            user_data: Cloud-init script content.

        Returns:
            The ``droplet`` object from the DO API response.
        """
        payload: dict[str, Any] = {
            "name": name,
            "region": region,
            "size": size,
            "image": image,
        }
        if ssh_key_ids:
            payload["ssh_keys"] = ssh_key_ids
        if tags:
            payload["tags"] = tags
        if user_data:
            payload["user_data"] = user_data

        return self._create(
            "/droplets",
            payload,
            "droplet",
            "droplet",
            lambda: [d for d in self.list_droplets() if d.get("name") == name],
        )

    def get_droplet(self, droplet_id: int) -> dict[str, Any]:
        """Retrieve a single Droplet by ID.

        Returns:
            The ``droplet`` object from the DO API response.
        """
        response = self._request_with_retry("GET", f"/droplets/{droplet_id}")
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any], data["droplet"])

    def list_droplets(self, tag_name: str | None = None) -> list[dict[str, Any]]:
        """List all Droplets, optionally filtered by tag.

        Paginates through all pages (``per_page=200``).

        Args:
            tag_name: If provided, only Droplets with this tag are returned.

        Returns:
            List of ``droplet`` objects.
        """
        first_page: dict[str, Any] = {"per_page": 200}
        if tag_name:
            first_page["tag_name"] = tag_name
        params: dict[str, Any] | None = first_page

        droplets: list[dict[str, Any]] = []
        path: str | None = "/droplets"

        while path:
            response = self._request_with_retry("GET", path, params=params)
            data: dict[str, Any] = response.json()
            droplets.extend(data.get("droplets", []))
            # None, not {}: an empty dict would replace the next-page URL's query string
            params = None
            path = _next_page_path(data)

        return droplets

    def delete_droplet(self, droplet_id: int) -> None:
        """Delete a Droplet by ID.

        The DO API returns 204 No Content on success.
        """
        self._request_with_retry("DELETE", f"/droplets/{droplet_id}")

    def droplet_action(self, droplet_id: int, action_type: str, **kwargs: Any) -> dict[str, Any]:
        """Perform an action on a Droplet.

        Args:
            droplet_id: Droplet to target.
            action_type: Action type string (e.g. ``"power_off"``).
            **kwargs: Additional action parameters merged into the request body.

        Returns:
            The ``action`` object from the DO API response.
        """
        payload: dict[str, Any] = {"type": action_type, **kwargs}
        response = self._request_with_retry("POST", f"/droplets/{droplet_id}/actions", json=payload)
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any], data["action"])

    def power_off(self, droplet_id: int) -> dict[str, Any]:
        """Power off a Droplet (graceful shutdown)."""
        return self.droplet_action(droplet_id, "power_off")

    def power_on(self, droplet_id: int) -> dict[str, Any]:
        """Power on a Droplet."""
        return self.droplet_action(droplet_id, "power_on")

    def resize(self, droplet_id: int, size: str) -> dict[str, Any]:
        """Resize a Droplet to a new size slug."""
        return self.droplet_action(droplet_id, "resize", size=size)

    # ------------------------------------------------------------------
    # Action polling
    # ------------------------------------------------------------------

    def get_action(self, action_id: int) -> dict[str, Any]:
        """Retrieve an action by ID.

        Returns:
            The ``action`` object from the DO API response.
        """
        response = self._request_with_retry("GET", f"/actions/{action_id}")
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any], data["action"])

    def wait_for_action(
        self,
        action_id: int,
        *,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Poll an action until ``status='completed'``.

        Args:
            action_id: DO action ID to poll.
            poll_interval: Seconds between polls. Default: 5.
            timeout: Maximum seconds to wait. Default: 300.

        Returns:
            The completed ``action`` object.

        Raises:
            CloudError: If action enters ``'errored'`` status or timeout
                elapses before completion.
        """
        deadline = time.monotonic() + timeout
        while True:
            action = self.get_action(action_id)
            status = action.get("status", "")

            if status == "completed":
                return action

            if status == "errored":
                raise CloudError(
                    f"Action {action_id} entered 'errored' status.",
                    error_code="DANGO-D013",
                )

            if time.monotonic() >= deadline:
                raise CloudError(
                    f"Action {action_id} timed out after {timeout}s (last status: {status!r}).",
                    error_code="DANGO-D014",
                )

            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # SSH Key operations
    # ------------------------------------------------------------------

    def upload_ssh_key(self, name: str, public_key: str) -> dict[str, Any]:
        """Upload an SSH public key to the DO account.

        Args:
            name: Identifying label for the key.
            public_key: OpenSSH public key string.

        Returns:
            The ``ssh_key`` object from the DO API response.
        """
        payload = {"name": name, "public_key": public_key}
        return self._create(
            "/account/keys",
            payload,
            "ssh_key",
            "SSH key",
            lambda: [
                k
                for k in self.list_ssh_keys()
                if k.get("name") == name and same_public_key(k, public_key)
            ],
        )

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        """List all SSH keys on the DO account.

        Paginates through all pages (``per_page=200``).

        Returns:
            List of ``ssh_key`` objects.
        """
        keys: list[dict[str, Any]] = []
        path: str | None = "/account/keys"
        params: dict[str, Any] | None = {"per_page": 200}

        while path:
            response = self._request_with_retry("GET", path, params=params)
            data: dict[str, Any] = response.json()
            keys.extend(data.get("ssh_keys", []))
            params = None
            path = _next_page_path(data)

        return keys

    def delete_ssh_key(self, key_id_or_fingerprint: str | int) -> None:
        """Delete an SSH key by ID or fingerprint.

        The DO API returns 204 No Content on success.
        """
        self._request_with_retry("DELETE", f"/account/keys/{key_id_or_fingerprint}")

    # ------------------------------------------------------------------
    # Firewall operations
    # ------------------------------------------------------------------

    def create_firewall(
        self,
        name: str,
        inbound_rules: list[dict[str, Any]],
        outbound_rules: list[dict[str, Any]],
        droplet_ids: list[int] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Firewall.

        Args:
            name: Firewall name.
            inbound_rules: List of inbound rule dicts per the DO API schema.
            outbound_rules: List of outbound rule dicts per the DO API schema.
            droplet_ids: Droplet IDs to associate with the Firewall.
            tags: Tag names to associate (all tagged Droplets are covered).

        Returns:
            The ``firewall`` object from the DO API response.
        """
        payload: dict[str, Any] = {
            "name": name,
            "inbound_rules": inbound_rules,
            "outbound_rules": outbound_rules,
        }
        if droplet_ids:
            payload["droplet_ids"] = droplet_ids
        if tags:
            payload["tags"] = tags

        return self._create(
            "/firewalls",
            payload,
            "firewall",
            "firewall",
            lambda: [f for f in list_firewalls(self) if f.get("name") == name],
        )

    def add_droplets_to_firewall(self, firewall_id: str, droplet_ids: list[int]) -> None:
        """Associate one or more Droplets with an existing Firewall.

        The DO API returns 204 No Content on success.
        """
        payload = {"droplet_ids": droplet_ids}
        self._request_with_retry("POST", f"/firewalls/{firewall_id}/droplets", json=payload)

    def get_firewall(self, firewall_id: str) -> dict[str, Any]:
        """Retrieve a single Firewall by ID.

        Args:
            firewall_id: UUID of the Firewall to retrieve.

        Returns:
            The ``firewall`` object from the DO API response.
        """
        response = self._request_with_retry("GET", f"/firewalls/{firewall_id}")
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any], data["firewall"])

    def update_firewall(
        self,
        firewall_id: str,
        name: str,
        inbound_rules: list[dict[str, Any]],
        outbound_rules: list[dict[str, Any]],
        droplet_ids: list[int] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace a Firewall's configuration (full replacement, not partial update).

        Args:
            firewall_id: UUID of the Firewall to update.
            name: Firewall name.
            inbound_rules: New inbound rule list (replaces existing).
            outbound_rules: New outbound rule list (replaces existing).
            droplet_ids: Droplet IDs to associate. Pass empty list to remove all;
                pass ``None`` to leave associations unchanged (omits field).
            tags: Tag names to associate (optional).

        Returns:
            The updated ``firewall`` object from the DO API response.
        """
        payload: dict[str, Any] = {
            "name": name,
            "inbound_rules": inbound_rules,
            "outbound_rules": outbound_rules,
        }
        if droplet_ids is not None:
            payload["droplet_ids"] = droplet_ids
        if tags is not None:
            payload["tags"] = tags

        response = self._request_with_retry("PUT", f"/firewalls/{firewall_id}", json=payload)
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any], data["firewall"])

    def delete_firewall(self, firewall_id: str) -> None:
        """Delete a Firewall by ID.

        The DO API returns 204 No Content on success.
        """
        self._request_with_retry("DELETE", f"/firewalls/{firewall_id}")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _next_page_path(data: dict[str, Any]) -> str | None:
    """Extract the next-page path from a DO pagination response.

    DO pagination uses ``links.pages.next`` with an absolute URL.  We strip
    the base URL to get a relative path for ``_request_with_retry``.

    Returns:
        Relative path string (e.g. ``"/droplets?page=2&per_page=200"``), or
        ``None`` if there are no more pages.
    """
    try:
        next_url: str = data["links"]["pages"]["next"]
    except (KeyError, TypeError):
        return None

    if next_url.startswith(_BASE_URL):
        return next_url[len(_BASE_URL) :]
    return None
