"""dango/web/routes/metabase_cookies.py

Reading the ``metabase.SESSION`` Set-Cookie that Metabase sends to the proxy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

MB_SESSION_COOKIE = "metabase.SESSION"


def upstream_session_cookie(response: httpx.Response) -> tuple[str, bool] | None:
    """The last ``metabase.SESSION`` Set-Cookie of an upstream response: ``(value, is_expiry)``."""
    found: tuple[str, bool] | None = None
    for raw in response.headers.get_list("set-cookie"):
        first, *attrs = [part.strip() for part in raw.split(";")]
        name, _, value = first.partition("=")
        if name != MB_SESSION_COOKIE:
            continue
        expired = value == ""
        for attr in attrs:
            key, _, attr_value = attr.partition("=")
            key = key.lower()
            try:
                if key == "max-age" and int(attr_value) <= 0:
                    expired = True
                elif key == "expires":
                    when = parsedate_to_datetime(attr_value)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    expired = expired or when <= datetime.now(tz=timezone.utc)
            except (TypeError, ValueError):
                continue
        found = (value, expired)
    return found
