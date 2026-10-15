"""dango/web/middleware/origin.py

Origin/Host/peer checks shared by the auth middleware's HTTP and WebSocket paths.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_LOOPBACK_ALIASES: frozenset[str] = frozenset({"localhost", "127.0.0.1"})


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    """Return the first value of header *name* (lower-case bytes), decoded."""
    for header_name, header_value in headers:
        if header_name == name:
            return header_value.decode("latin-1")
    return None


def is_same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    """Check a request's ``Origin`` header against its ``Host`` header.

    Used for WebSocket handshakes and for the HTTP auth-disabled mode.
    No ``Origin`` header (non-browser client such as curl or the CLI) -> True;
    callers still apply their own credential rules.  Otherwise the origin's host and port (default port
    taken from its scheme) must equal the ``Host`` header's, where ``localhost``
    and ``127.0.0.1`` count as the same host.  ``X-Forwarded-Host`` is not trusted.
    """
    origin = _header(headers, b"origin")
    if origin is None:
        return True
    host = _header(headers, b"host")
    if not host:
        return False
    try:
        parsed_origin = urlsplit(origin.strip())
        scheme = parsed_origin.scheme.lower()
        default_port = _DEFAULT_PORTS.get(scheme)
        origin_host = parsed_origin.hostname
        if default_port is None or not origin_host:
            return False  # e.g. "null" or an unknown scheme
        origin_port = parsed_origin.port or default_port
        parsed_host = urlsplit("//" + host.strip())
        host_name = parsed_host.hostname
        if not host_name:
            return False
        host_port = parsed_host.port or default_port
    except ValueError:
        return False  # malformed port

    if origin_port != host_port:
        return False
    if origin_host == host_name:
        return True
    return origin_host in _LOOPBACK_ALIASES and host_name in _LOOPBACK_ALIASES


def _is_loopback_address(value: str) -> bool:
    """True for an IP literal in 127.0.0.0/8 or ``::1`` (incl. IPv4-mapped)."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def _host_name(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Lower-cased host name from the ``Host`` header (port and IPv6 brackets removed)."""
    host = _header(headers, b"host")
    if not host:
        return None
    try:
        return urlsplit("//" + host.strip()).hostname
    except ValueError:
        return None


def is_local_request(scope: Mapping[str, Any], local_domains: frozenset[str]) -> bool:
    """True only for a same-origin request from this machine to a local host name.

    All three must hold:

    - the direct TCP peer (``scope["client"]``, never ``X-Forwarded-For``) is
      loopback: rejects other machines when the server binds ``0.0.0.0``;
    - the ``Host`` name is ``localhost``, a loopback IP literal, or one of
      *local_domains* (the project's registered local nginx domains): rejects
      DNS-rebinding pages, whose Host is the attacker's name;
    - :func:`is_same_origin` passes: rejects other local origins.
    """
    client = scope.get("client")
    if not client or not _is_loopback_address(str(client[0])):
        return False
    headers = scope.get("headers", [])
    host = _host_name(headers)
    if host is None:
        return False
    if host != "localhost" and not _is_loopback_address(host) and host not in local_domains:
        return False
    return is_same_origin(headers)
