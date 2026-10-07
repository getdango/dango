"""dango/platform/cloud/size_availability.py

Checks which DigitalOcean regions offer a given Droplet size, so the deploy
wizard and provisioning never ask DO to create a Droplet that cannot exist.
"""

from __future__ import annotations

from typing import Any

from dango.exceptions import CloudError
from dango.platform.cloud.provisioning import RegionInfo, list_regions

_PROBE_TIMEOUT_SECONDS = 10.0


class SizeUnavailableError(CloudError):
    """The requested Droplet size is not offered in the requested region."""


def regions_for_size(sizes_payload: Any, slug: str) -> set[str]:
    """Return the region slugs where size *slug* can be created.

    Args:
        sizes_payload: The ``sizes`` list from ``GET /v2/sizes`` (or the whole
            response dict containing a ``sizes`` key).
        slug: Droplet size slug (e.g. ``"s-2vcpu-4gb"``).

    Returns:
        Region slugs.  Empty when the slug is unknown to DigitalOcean or the
        size is flagged ``available: false``.
    """
    if isinstance(sizes_payload, dict):
        sizes_payload = sizes_payload.get("sizes", [])
    for size in sizes_payload:
        if size.get("slug") != slug:
            continue
        if size.get("available") is False:
            return set()
        return {r for r in size.get("regions", []) if isinstance(r, str)}
    return set()


def offerable_regions(allowed_slugs: set[str]) -> list[RegionInfo]:
    """Return the known regions (static list order) whose slug is in *allowed_slugs*.

    Regions DigitalOcean reports but Dango has no metadata for are skipped.
    """
    return [r for r in list_regions() if r.slug in allowed_slugs]


def fetch_sizes(client: Any = None) -> list[dict[str, Any]] | None:
    """Fetch the DO size catalogue with a short timeout and no retries.

    Returns:
        The sizes list, or ``None`` if it could not be fetched for any reason
        (no token, network error, API error, malformed response).
    """
    try:
        if client is None:
            from dango.platform.cloud.digitalocean import DigitalOceanClient

            client = DigitalOceanClient(timeout=_PROBE_TIMEOUT_SECONDS, max_retries=0)
        sizes = client.list_sizes()
        if not isinstance(sizes, list):
            return None
        return sizes
    except Exception:  # noqa: BLE001 - availability is advisory; never crash callers
        return None


def check_size_in_region(region: str, size: str, *, client: Any = None) -> bool:
    """Verify before provisioning that *size* is offered in *region*.

    Always uses a short-timeout, no-retry probe client (see ``fetch_sizes``),
    so an API outage costs at most one 10 s request.

    Args:
        region: DO region slug.
        size: Droplet size slug.
        client: Optional client override (tests); default builds the probe client.

    Returns:
        ``True`` if verified available, ``False`` if the check itself failed
        (caller should warn and continue).

    Raises:
        SizeUnavailableError: If DO reports the size is not offered in *region*.
    """
    sizes = fetch_sizes(client)
    if sizes is None:
        return False
    allowed = regions_for_size(sizes, size)
    if region in allowed:
        return True
    if not allowed:
        raise SizeUnavailableError(
            f"Size '{size}' is not offered by DigitalOcean (unknown or unavailable slug). "
            "Nothing was created. Fix: pass --size <another size>."
        )
    raise SizeUnavailableError(
        f"Size '{size}' is not available in region '{region}'. "
        f"Regions offering it: {', '.join(sorted(allowed))}. Nothing was created. "
        "Fix: pass --region <one of the above> or --size <other>."
    )
