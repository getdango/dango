"""dango/platform/cloud/compose_versions.py

Version checks and client/server version notices for the compose helper switch.
"""

from __future__ import annotations

from typing import Any

from dango.logging import get_logger
from dango.platform.cloud.compose_scripts import release_of

_logger = get_logger(__name__)

#: First release that serves and understands the compose helper.
MIN_RELEASE = (1, 0, 14)


def helper_capable(version: str | None) -> bool:
    """True when *version* (a pre-release of 1.0.14 included) ships the compose helper."""
    release = release_of(version)
    return release is not None and release >= MIN_RELEASE


def below_helper_version(version: str | None) -> bool:
    """True when *version* predates the compose helper (a downgrade target that needs the group)."""
    release = release_of(version)
    return release is not None and release < MIN_RELEASE


def client_upgrade_line(local_version: str, target_version: str) -> str | None:
    """The line shown before a server upgrade when the local client is older than the target."""
    from packaging.version import InvalidVersion, Version

    try:
        if Version(local_version) >= Version(target_version):
            return None
    except InvalidVersion:
        return None
    return (
        "Upgrade your local Dango (pip install -U getdango) before upgrading the server; "
        f"this client is {local_version}, the target is {target_version}."
    )


def client_version_notice(ssh: Any, local_version: str) -> str | None:
    """Warning when the server records a newer Dango than this client (never raises)."""
    from dango.platform.cloud.server_status import _get_docker_access, client_version_warning

    try:
        _access, recorded = _get_docker_access(ssh)
        return client_version_warning(recorded, local_version)
    except Exception:
        _logger.warning("client_version_notice_failed", exc_info=True)
        return None


def server_version(ssh: Any) -> str:
    """The Dango version installed on the server (this client's, if it cannot be read)."""
    from dango import __version__
    from dango.platform.cloud import runtime_ownership as ro
    from dango.platform.cloud.server_status import _get_dango_version

    return ro.valid_version(_get_dango_version(ssh)) or str(__version__)
