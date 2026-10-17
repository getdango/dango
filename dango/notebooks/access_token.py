"""dango/notebooks/access_token.py

Per-start access token for the local Marimo server (file mode 0600, never in argv).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlencode


def token_file_path(project_root: Path) -> Path:
    """Path of the token file Marimo reads at startup."""
    return project_root / ".dango" / "state" / "marimo.token"


def create_token_file(project_root: Path) -> Path:
    """Write a fresh random token to the token file and return its path.

    The new file is created exclusively (``O_EXCL``, no symlink following,
    mode 0600) next to the target and atomically renamed over it, so a
    planted symlink or an older file is replaced rather than written through.
    """
    path = token_file_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read_token(project_root: Path) -> str | None:
    """Return the current token, or ``None`` when Marimo runs without one."""
    try:
        token = token_file_path(project_root).read_text().strip()
    except OSError:
        return None
    return token or None


def remove_token_file(project_root: Path) -> None:
    """Delete the token file (a stopped Marimo no longer needs it)."""
    token_file_path(project_root).unlink(missing_ok=True)


def marimo_open_url(project_root: Path, port: int, notebook_file: str | None = None) -> str:
    """URL to open local Marimo in a browser; carries the token so Marimo sets its cookie."""
    params: dict[str, str] = {}
    if notebook_file:
        params["file"] = notebook_file
    token = read_token(project_root)
    if token:
        params["access_token"] = token
    query = f"?{urlencode(params, safe='/.')}" if params else ""
    return f"http://localhost:{port}/{query}"
