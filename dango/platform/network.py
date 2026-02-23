"""dango/platform/network.py

Backwards-compatible re-export shim. Canonical location: dango.platform.local.network.

Manages shared nginx instance for clean URLs across multiple Dango projects.
"""

from dango.platform.local.network import HostsManager, NetworkConfig, NginxManager

__all__ = [
    "NetworkConfig",
    "NginxManager",
    "HostsManager",
]
