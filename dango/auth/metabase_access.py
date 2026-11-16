"""dango/auth/metabase_access.py

Per-user Metabase access state kept under ``.dango/state/metabase_access/``.

Two things are recorded so a role change cannot leave old Metabase privileges behind:

* the Metabase sessions Dango bridged for the user (so a role change can delete them), and
* a "pending" marker, set while a role change has not been fully applied in Metabase. The proxy
  and the session bridge refuse the user until a later sync clears it.

Plain files, no locking: the marker is created and removed atomically, and the session list is
append-only with an atomic rewrite. The web server and the CLI both use it.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from dango.logging import get_logger

logger = get_logger(__name__)

_MAX_SESSIONS_PER_USER = 20
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _user_dir(project_root: Path) -> Path:
    return project_root / ".dango" / "state" / "metabase_access"


def _file(project_root: Path, user_id: str, suffix: str) -> Path:
    if not _ID_RE.fullmatch(user_id):
        raise ValueError("invalid user id")
    return _user_dir(project_root) / f"{user_id}.{suffix}"


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def mark_pending(project_root: Path, user_id: str) -> None:
    """Record that *user_id*'s role change is not fully applied in Metabase."""
    _write_private(_file(project_root, user_id, "pending"), "pending\n")


def clear_pending(project_root: Path, user_id: str) -> None:
    """Remove the pending marker (the role change is fully applied)."""
    _file(project_root, user_id, "pending").unlink(missing_ok=True)


def is_pending(project_root: Path, user_id: str) -> bool:
    """True while a role change for *user_id* is not fully applied. Never raises."""
    try:
        return _file(project_root, user_id, "pending").exists()
    except (ValueError, TypeError):  # not a usable user id: nothing can be recorded for it
        return False


def record_session(project_root: Path, user_id: str, session_id: str) -> None:
    """Remember a Metabase session Dango bridged for *user_id* (best effort, never raises)."""
    if not _ID_RE.fullmatch(session_id):
        return
    try:
        kept = [*recorded_sessions(project_root, user_id), session_id]
        _write_private(
            _file(project_root, user_id, "sessions"),
            "".join(f"{sid}\n" for sid in kept[-_MAX_SESSIONS_PER_USER:]),
        )
    except Exception:
        logger.warning("metabase_session_record_failed", exc_info=True)


def recorded_sessions(project_root: Path, user_id: str) -> list[str]:
    """Metabase session ids recorded for *user_id* (oldest first)."""
    try:
        text = _file(project_root, user_id, "sessions").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    return [line for line in text.splitlines() if _ID_RE.fullmatch(line)]


def forget_sessions(project_root: Path, user_id: str, session_ids: list[str]) -> None:
    """Drop *session_ids* from the record (they were deleted in Metabase)."""
    remaining = [s for s in recorded_sessions(project_root, user_id) if s not in session_ids]
    path = _file(project_root, user_id, "sessions")
    if remaining:
        _write_private(path, "".join(f"{sid}\n" for sid in remaining))
    else:
        path.unlink(missing_ok=True)
