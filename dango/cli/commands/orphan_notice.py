"""dango/cli/commands/orphan_notice.py

Warn about DigitalOcean droplets tagged ``dango`` that this project's cloud.yml does not name.

A deploy killed hard (no chance to clean up) leaves such a droplet running and billing. This is a
warning only: it never deletes anything and never blocks. It needs a DigitalOcean token; with none,
or if the API call fails, it prints nothing and logs at debug level.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.markup import escape

from dango.cli import console
from dango.logging import get_logger
from dango.platform.cloud.provisioning import _DANGO_TAG

_logger = get_logger(__name__)


def _default_client(project_root: Path) -> Any | None:
    """Return a DigitalOcean client, or ``None`` when no token is configured."""
    from dango.config.cloud_credentials import get_do_token
    from dango.platform.cloud.digitalocean import DigitalOceanClient

    token = get_do_token(project_root=project_root)
    if not token:
        return None
    return DigitalOceanClient(token=token)


def _tracked_droplet_id(project_root: Path) -> str | None:
    from dango.config.loader import ConfigLoader

    cloud_cfg = ConfigLoader(project_root).load_cloud_config()
    droplet_id = getattr(cloud_cfg, "droplet_id", None)
    return None if droplet_id is None else str(droplet_id)


def warn_untracked_droplets(
    project_root: Path,
    *,
    client_factory: Callable[[], Any | None] | None = None,
) -> None:
    """Print a warning listing ``dango``-tagged droplets that cloud.yml does not name.

    Never raises and never deletes: any failure is logged at debug level and ignored.
    """
    try:
        client = client_factory() if client_factory else _default_client(project_root)
        if client is None:
            _logger.debug("orphan_check_no_token")
            return
        tracked = _tracked_droplet_id(project_root)
        untracked = [
            d for d in client.list_droplets(tag_name=_DANGO_TAG) if str(d.get("id")) != tracked
        ]
    except Exception as exc:
        _logger.debug("orphan_check_failed", error=str(exc))
        return

    if not untracked:
        return

    noun = "droplet" if len(untracked) == 1 else "droplets"
    console.print(
        f"[yellow]Warning:[/yellow] DigitalOcean has {len(untracked)} {noun} tagged "
        f"'{_DANGO_TAG}' that this project's cloud.yml does not name:",
        highlight=False,
    )
    for droplet in untracked:
        console.print(
            f"  id {escape(str(droplet.get('id')))}  name {escape(str(droplet.get('name')))}  "
            f"created {escape(str(droplet.get('created_at')))}",
            highlight=False,
        )
    console.print(
        "  If they are leftovers, delete them in the DigitalOcean console "
        "(a deploy killed hard leaves one). They may belong to another Dango project; "
        "nothing was deleted.",
        highlight=False,
    )
