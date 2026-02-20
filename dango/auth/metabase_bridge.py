"""dango/auth/metabase_bridge.py

Async Metabase session bridging for per-user SSO.

Provides functions to create and destroy Metabase sessions using a user's
encrypted Metabase password, and to ensure a user is synced to Metabase
before first login.  All functions catch exceptions and return
``None``/``False`` — Metabase being unreachable never blocks Dango auth.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import yaml

from dango.auth.models import User

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _load_metabase_url(project_root: Path) -> str | None:
    """Read the Metabase URL from ``.dango/metabase.yml``.

    Returns ``None`` if the file is missing or the key is absent.
    """
    path = project_root / ".dango" / "metabase.yml"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f)
        url = data.get("metabase_url")
        return str(url) if url else None
    except Exception:
        logger.debug("Failed to load metabase URL", exc_info=True)
        return None


async def get_metabase_url(project_root: Path) -> str | None:
    """Return the configured Metabase URL (async wrapper).

    Reads ``.dango/metabase.yml`` via ``asyncio.to_thread`` so that the
    blocking file I/O does not stall the event loop.
    """
    return await asyncio.to_thread(_load_metabase_url, project_root)


async def bridge_metabase_login(
    user: User,
    project_root: Path,
    metabase_url: str | None = None,
) -> str | None:
    """Create a Metabase session for *user* using their encrypted password.

    Returns the Metabase session ID on success, or ``None`` if bridging
    cannot proceed (no credentials, Metabase down, etc.).
    """
    try:
        if user.metabase_password_enc is None or user.metabase_user_id is None:
            logger.debug("Metabase bridge skip: no credentials for %s", user.email)
            return None

        if metabase_url is None:
            metabase_url = await get_metabase_url(project_root)
        if metabase_url is None:
            logger.debug("Metabase bridge skip: no URL configured")
            return None

        from dango.auth.metabase_sync import decrypt_metabase_password

        password = decrypt_metabase_password(user.metabase_password_enc, project_root)

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{metabase_url}/api/session",
                json={"username": user.email, "password": password},
            )

        if resp.status_code == 200:
            session_id = resp.json().get("id")
            if isinstance(session_id, str) and session_id:
                logger.info("Metabase bridge login for %s: %s...", user.email, session_id[:8])
                return session_id

        logger.warning(
            "Metabase bridge login failed for %s (HTTP %s)", user.email, resp.status_code
        )
        return None

    except Exception:
        logger.warning("Metabase bridge login error for %s", user.email, exc_info=True)
        return None


async def bridge_metabase_logout(
    session_id: str,
    project_root: Path,
    metabase_url: str | None = None,
) -> bool:
    """Invalidate a Metabase session (server-side DELETE).

    Returns ``True`` if Metabase confirmed the session was deleted.
    """
    try:
        if metabase_url is None:
            metabase_url = await get_metabase_url(project_root)
        if metabase_url is None:
            return False

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{metabase_url}/api/session",
                headers={"X-Metabase-Session": session_id},
            )

        if resp.status_code in (200, 204):
            logger.info("Metabase bridge logout: %s...", session_id[:8])
            return True

        logger.warning("Metabase bridge logout failed (HTTP %s)", resp.status_code)
        return False

    except Exception:
        logger.warning("Metabase bridge logout error", exc_info=True)
        return False


async def ensure_metabase_synced(
    db_path: Path,
    user_id: str,
    project_root: Path,
    metabase_url: str,
) -> None:
    """Sync a Dango user to Metabase in a background thread.

    Wraps the synchronous ``sync_user_to_metabase`` so it can be called
    from async code without blocking the event loop.  Exceptions are
    caught and logged — this function never raises.
    """
    try:
        from dango.auth.metabase_sync import sync_user_to_metabase

        await asyncio.to_thread(sync_user_to_metabase, db_path, user_id, project_root, metabase_url)
    except Exception:
        logger.warning("Metabase sync on startup failed for user %s", user_id, exc_info=True)
