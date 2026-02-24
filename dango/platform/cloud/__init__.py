"""dango/platform/cloud/__init__.py

Cloud deployment platform components.

Populated by TASK-022+ (cloud provisioning, Caddy, remote sync).
"""

from .digitalocean import DigitalOceanClient
from .spaces import SpacesClient
from .ssh import CommandResult, SSHManager

__all__ = ["CommandResult", "DigitalOceanClient", "SpacesClient", "SSHManager"]
