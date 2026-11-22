"""tests/factories/network_guard.py

Context manager that makes every non-loopback connection or name lookup fail and records the target, so a test
that reaches for the network fails with the address instead of silently going out.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


def _host(address: Any) -> str | None:
    """Host of an AF_INET/AF_INET6 address tuple, None for a unix socket path or anything else."""
    if isinstance(address, tuple) and address:
        host = address[0]
        return host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    return None


@contextmanager
def blocked_network() -> Iterator[list[str]]:
    """Yield the list of blocked targets (`connect host:port` / `resolve host`); loopback and unix sockets pass."""
    attempts: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        host = _host(address)
        if host is None or host in LOOPBACK:
            return real_connect(self, address)
        attempts.append(f"connect {host}:{address[1]}")
        raise OSError(f"network disabled in tests: connect to {host}:{address[1]}")

    def connect_ex(self: socket.socket, address: Any) -> int:
        host = _host(address)
        if host is None or host in LOOPBACK:
            return real_connect_ex(self, address)
        attempts.append(f"connect {host}:{address[1]}")
        raise OSError(f"network disabled in tests: connect to {host}:{address[1]}")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if name is None or name in LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        attempts.append(f"resolve {name}")
        raise OSError(f"network disabled in tests: resolve {name}")

    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        patch.object(socket, "getaddrinfo", getaddrinfo),
    ):
        yield attempts
