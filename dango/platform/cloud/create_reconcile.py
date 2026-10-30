"""dango/platform/cloud/create_reconcile.py

Reconciliation for DigitalOcean create calls whose outcome is unknown.

A create POST (droplet, firewall, SSH key) that the API may have accepted but
answered with a 5xx, a read/write timeout or a dropped connection must not be
sent again: that is how a second, untracked droplet gets created.  The client
instead looks the resource up by its unique name and adopts it when exactly one
match exists.

Only requests that provably did not take effect are repeated by the client
(HTTP 429, and connection failures before the request was sent).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from dango.exceptions import CloudError

__all__ = ["CreateOutcomeUnknown", "reconcile_create", "list_firewalls", "same_public_key"]


# One repeat lookup after this pause (same as the 5 s provisioning poll interval).
_RECHECK_DELAY = 5.0


class CreateOutcomeUnknown(Exception):
    """A create POST ended without an answer that shows whether it took effect.

    Raised by the DigitalOcean client's retry helper; never escapes the client.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def same_public_key(listed: dict[str, Any], public_key: str) -> bool:
    """Return True when a listed SSH key carries the same key material (comment ignored)."""
    listed_key = listed.get("public_key")
    if not isinstance(listed_key, str):
        return True
    return listed_key.split()[:2] == public_key.split()[:2]


def list_firewalls(client: Any) -> list[dict[str, Any]]:
    """List all firewalls on the account (follows pagination)."""
    from dango.platform.cloud.digitalocean import _next_page_path

    firewalls: list[dict[str, Any]] = []
    path: str | None = "/firewalls"
    params: dict[str, Any] | None = {"per_page": 200}
    while path:
        data: dict[str, Any] = client._request_with_retry("GET", path, params=params).json()
        firewalls.extend(data.get("firewalls", []))
        params = None  # None, not {}: an empty dict would replace the next-page query string
        path = _next_page_path(data)
    return firewalls


def reconcile_create(
    kind: str,
    name: str,
    detail: str,
    find: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Adopt the single resource called *name*, or raise.

    Args:
        kind: Resource kind for messages (``"droplet"``, ``"firewall"``, ``"SSH key"``).
        name: The unique name the create request carried.
        detail: Why the outcome is unknown (for example ``"HTTP 502"``).
        find: Returns the existing resources whose name matches.

    Raises:
        CloudError: No match after two lookups (outcome unknown), several matches
            (IDs listed, nothing adopted), or the lookup itself failed.
    """
    unconfirmed = (
        f"The DigitalOcean {kind} create request for '{name}' could not be confirmed ({detail})"
    )
    try:
        matches = find()
        if not matches:
            # The record can appear just after the lost answer; look once more.
            time.sleep(_RECHECK_DELAY)
            matches = find()
    except (CloudError, httpx.HTTPError) as exc:
        raise CloudError(
            f"{unconfirmed} and the lookup failed: {exc}. "
            f"A {kind} named '{name}' may exist; check the DigitalOcean console."
        ) from exc
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CloudError(
            f"{unconfirmed}; the outcome is unknown. No {kind} named '{name}' was found yet, but it "
            "may still appear. Check the DigitalOcean console and delete it if it does before "
            "trying again."
        )
    ids = ", ".join(str(m.get("id")) for m in matches)
    raise CloudError(
        f"{unconfirmed}; {len(matches)} {kind}s are named '{name}' (IDs: {ids}), so none was "
        "adopted. Remove the extras in the DigitalOcean console before trying again."
    )
