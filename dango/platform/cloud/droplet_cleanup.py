"""dango/platform/cloud/droplet_cleanup.py

Best-effort deletion of a droplet created by a failed provisioning/migration, without hiding a failed delete.
"""

from __future__ import annotations

from typing import Any

from dango.exceptions import DangoError
from dango.logging import get_logger

_logger = get_logger(__name__)


def _annotate(exc: BaseException, text: str) -> None:
    """Attach ``text`` to ``exc`` so the user sees it wherever the error is printed.

    ``DangoError`` subclasses are printed through ``str()`` / ``user_message``, which
    ``add_note`` (3.11+) does not touch, so their message is extended directly. Other
    exceptions get ``add_note`` when available (3.11+), else a rebuilt single-string message.
    """
    if isinstance(exc, DangoError):
        exc.args = (f"{exc}\n{text}",)
        exc.user_message = f"{exc.user_message}\n{text}"
        return
    add_note = getattr(exc, "add_note", None)
    if add_note is not None:
        add_note(text)
    elif len(exc.args) == 1 and isinstance(exc.args[0], str):
        exc.args = (f"{exc.args[0]}\n{text}",)


def delete_droplet_or_annotate(client: Any, droplet_id: int, original: BaseException) -> None:
    """Delete ``droplet_id``; if that fails, log it and tell the user it may still be billed.

    Never raises: the caller re-raises ``original``.
    """
    try:
        client.delete_droplet(droplet_id)
    except Exception:
        _logger.error("orphaned_droplet", droplet_id=droplet_id, exc_info=True)
        _annotate(
            original,
            f"Cleanup failed: droplet {droplet_id} may still be running and billed. "
            f"Delete it with: doctl compute droplet delete {droplet_id}  "
            "(or in the DigitalOcean console).",
        )


def note_old_droplet_left(
    original: BaseException, new_id: int | None, old_id: int, old_destroyed: bool = False
) -> None:
    """Tell the user an interrupted migration finished switching; name the old droplet if it remains."""
    text = f"The new server (droplet {new_id}) is live and recorded in cloud.yml."
    if not old_destroyed:
        text += (
            f" The old droplet {old_id} may still be running and billed."
            f" Delete it with: doctl compute droplet delete {old_id}  "
            "(or in the DigitalOcean console)."
        )
    _annotate(original, text)
