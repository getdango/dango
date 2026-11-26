"""dango/cli/helpers/bind_host.py

The bind address for ``dango web`` and ``dango serve``, decided in one place.

With local authentication explicitly turned off (``dango auth disable``) and no ``--host`` given,
both commands listen on 127.0.0.1 only. An explicit ``--host`` always wins. Cloud servers and
projects with authentication on (or no readable toggle) keep the caller's default.

Local code: runs on Windows, macOS and Linux. Only IPv4 loopback is chosen here; IPv6 ``::`` is
never used. Messages are plain ASCII.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class BindDecision:
    """The host to bind and an optional single line to show the user."""

    host: str
    message: str | None = None
    is_warning: bool = False


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and any loopback IP address (``127.x.x.x``, ``::1``)."""
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def resolve_bind_host(
    project_root: Path, explicit_host: str | None, *, default_host: str
) -> BindDecision:
    """Decide the address to bind.

    ``explicit_host`` is the ``--host`` value (None when not given); ``default_host`` is what the
    command binds when nothing special applies.
    """
    from dango.auth.admin import is_auth_explicitly_disabled
    from dango.config.helpers import is_running_on_cloud

    if is_running_on_cloud():
        return BindDecision(explicit_host if explicit_host is not None else default_host)

    if explicit_host is not None:
        if is_loopback_host(explicit_host) or not is_auth_explicitly_disabled(project_root):
            return BindDecision(explicit_host)
        return BindDecision(
            explicit_host,
            f"WARNING: Local authentication is turned off and --host {explicit_host} listens on "
            "the network: anyone who can reach this computer can use this project without "
            "logging in.",
            is_warning=True,
        )

    if is_auth_explicitly_disabled(project_root):
        return BindDecision(
            LOOPBACK_HOST,
            f"Local authentication is turned off, so Dango is listening only on this computer "
            f"({LOOPBACK_HOST}). To listen on the network, pass --host 0.0.0.0; with "
            "authentication off that exposes this project to the network.",
        )
    return BindDecision(default_host)
