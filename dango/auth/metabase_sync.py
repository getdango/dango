"""dango/auth/metabase_sync.py

Synchronize Dango users to Metabase and manage role-based group membership.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

import requests
import yaml

from dango.auth.database import get_user_by_id, list_users, update_user
from dango.auth.models import Role, UserUpdate
from dango.security.token_storage import SecureTokenStorage

logger = logging.getLogger(__name__)

_EDITORS_GROUP_NAME = "Dango Editors"
_TIMEOUT = 10
_ALL_USERS_GROUP_ID = 1
_ADMIN_GROUP_ID = 2


def _load_metabase_credentials(project_root: Path) -> dict[str, Any] | None:
    """Read ``.dango/metabase.yml``. Returns ``None`` if missing."""
    path = project_root / ".dango" / "metabase.yml"
    if not path.exists():
        logger.warning("metabase.yml not found: %s", path)
        return None
    try:
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f)
        return data
    except Exception:
        logger.exception("Failed to read metabase.yml")
        return None


def _get_admin_session(metabase_url: str, project_root: Path) -> str | None:
    """Authenticate as the Metabase admin and return a session token."""
    creds = _load_metabase_credentials(project_root)
    if creds is None:
        return None
    admin = creds.get("admin", {})
    email, password = admin.get("email"), admin.get("password")
    if not email or not password:
        logger.error("Metabase admin credentials missing in metabase.yml")
        return None
    try:
        resp = requests.post(
            f"{metabase_url}/api/session",
            json={"username": email, "password": password},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("id")  # type: ignore[no-any-return]
        logger.error("Metabase login failed (HTTP %s)", resp.status_code)
    except Exception:
        logger.exception("Metabase login request failed")
    return None


def _mb_get(url: str, session: str, path: str) -> Any:
    """GET a Metabase API endpoint. Returns parsed JSON or ``None``."""
    try:
        r = requests.get(f"{url}{path}", headers={"X-Metabase-Session": session}, timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        logger.exception("Metabase GET %s failed", path)
        return None


def _mb_post(url: str, session: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """POST to a Metabase API endpoint. Returns parsed JSON or ``None``."""
    try:
        r = requests.post(
            f"{url}{path}",
            headers={"X-Metabase-Session": session},
            json=body,
            timeout=_TIMEOUT,
        )
        return r.json() if r.status_code in (200, 201) else None
    except Exception:
        logger.exception("Metabase POST %s failed", path)
        return None


def _mb_put(url: str, session: str, path: str, body: dict[str, Any]) -> Any:
    """PUT to a Metabase API endpoint. Returns parsed JSON or ``None``."""
    try:
        r = requests.put(
            f"{url}{path}",
            headers={"X-Metabase-Session": session},
            json=body,
            timeout=_TIMEOUT,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        logger.exception("Metabase PUT %s failed", path)
        return None


def _mb_delete(url: str, session: str, path: str) -> bool:
    """DELETE a Metabase API resource. Returns ``True`` on success."""
    try:
        r = requests.delete(
            f"{url}{path}", headers={"X-Metabase-Session": session}, timeout=_TIMEOUT
        )
        return r.status_code in (200, 204)
    except Exception:
        logger.exception("Metabase DELETE %s failed", path)
        return False


def _get_database_id(project_root: Path) -> int | None:
    """Return the DuckDB database ID from ``metabase.yml``."""
    creds = _load_metabase_credentials(project_root)
    if creds is None:
        return None
    db_id = creds.get("database", {}).get("id")
    return int(db_id) if db_id is not None else None


def generate_metabase_password() -> str:
    """Generate a random password for a Metabase user (never shown to user)."""
    return secrets.token_urlsafe(32)


def encrypt_metabase_password(password: str, project_root: Path) -> str:
    """Encrypt a Metabase password using ``SecureTokenStorage``."""
    return SecureTokenStorage(project_root).encrypt_token({"metabase_password": password})


def decrypt_metabase_password(encrypted: str, project_root: Path) -> str:
    """Decrypt a Metabase password for session bridging (TASK-019)."""
    return SecureTokenStorage(project_root).decrypt_token(encrypted)[  # type: ignore[no-any-return]
        "metabase_password"
    ]


def ensure_metabase_groups(
    metabase_url: str,
    project_root: Path,
    session: str | None = None,
) -> dict[str, int] | None:
    """Ensure Metabase permission groups exist and are configured.

    Returns ``{"editors": <id>, "admin": 2, "all_users": 1}`` or ``None``.
    """
    if session is None:
        session = _get_admin_session(metabase_url, project_root)
    if session is None:
        return None

    groups_resp = _mb_get(metabase_url, session, "/api/permissions/group")
    if not isinstance(groups_resp, list):
        return None

    editors_id: int | None = None
    for group in groups_resp:
        if group.get("name") == _EDITORS_GROUP_NAME:
            editors_id = group["id"]
            break
    if editors_id is None:
        created = _mb_post(
            metabase_url,
            session,
            "/api/permissions/group",
            {"name": _EDITORS_GROUP_NAME},
        )
        if created is None:
            return None
        editors_id = created["id"]

    db_id = _get_database_id(project_root)
    if db_id is None:
        return None

    graph = _mb_get(metabase_url, session, "/api/permissions/graph")
    if not isinstance(graph, dict):
        return None

    groups = graph.get("groups", {})
    db_key = str(db_id)
    groups.setdefault(str(_ALL_USERS_GROUP_ID), {})[db_key] = {
        "view-data": "unrestricted",
        "create-queries": "no",
    }
    groups.setdefault(str(editors_id), {})[db_key] = {
        "view-data": "unrestricted",
        "create-queries": "query-builder-and-native",
    }

    if _mb_put(metabase_url, session, "/api/permissions/graph", graph) is None:
        return None
    return {
        "editors": editors_id,
        "admin": _ADMIN_GROUP_ID,
        "all_users": _ALL_USERS_GROUP_ID,
    }


def _get_role_groups(role: Role, group_ids: dict[str, int]) -> list[int]:
    """Return the target Metabase group IDs for a Dango role."""
    if role == Role.ADMIN:
        return [group_ids["admin"]]
    if role == Role.EDITOR:
        return [group_ids["editors"]]
    return []  # Viewer — "All Users" is automatic


def _sync_user_groups(
    metabase_url: str,
    session: str,
    mb_user_id: int,
    target_groups: list[int],
    group_ids: dict[str, int],
) -> bool:
    """Sync a Metabase user's group memberships to *target_groups*."""
    user_resp = _mb_get(metabase_url, session, f"/api/user/{mb_user_id}")
    if not isinstance(user_resp, dict):
        return False

    current_gids: set[int] = set()
    for entry in user_resp.get("group_ids", []):
        if isinstance(entry, dict):
            current_gids.add(entry.get("id", 0))
        elif isinstance(entry, int):
            current_gids.add(entry)

    target_set = set(target_groups)
    skip = {group_ids["all_users"]}
    for gid in target_set - current_gids:
        if gid not in skip:
            _mb_post(
                metabase_url,
                session,
                "/api/permissions/membership",
                {"group_id": gid, "user_id": mb_user_id},
            )
    for gid in current_gids - target_set:
        if gid in skip:
            continue
        grp = _mb_get(metabase_url, session, f"/api/permissions/group/{gid}")
        if not isinstance(grp, dict):
            continue
        for member in grp.get("members", []):
            if member.get("user_id") == mb_user_id:
                mid = member.get("membership_id")
                if mid is not None:
                    _mb_delete(metabase_url, session, f"/api/permissions/membership/{mid}")
                break
    return True


def _apply_role(url: str, session: str, uid: int, role: Role, gids: dict[str, int]) -> None:
    """Sync group membership and superuser flag for a Metabase user."""
    # Revoke superuser first on demotion (is_superuser bypasses all permission checks)
    _mb_put(url, session, f"/api/user/{uid}", {"is_superuser": role == Role.ADMIN})
    _sync_user_groups(url, session, uid, _get_role_groups(role, gids), gids)


def sync_user_to_metabase(
    db_path: Path, user_id: str, project_root: Path, metabase_url: str
) -> int | None:
    """Create or verify a Metabase account for a Dango user.

    Returns the Metabase user ID, or ``None`` on failure.
    """
    try:
        user = get_user_by_id(db_path, user_id)
        if user is None:
            logger.error("User %s not found in auth.db", user_id)
            return None

        session = _get_admin_session(metabase_url, project_root)
        if session is None:
            return None

        if user.metabase_user_id is not None:
            verify = _mb_get(metabase_url, session, f"/api/user/{user.metabase_user_id}")
            if isinstance(verify, dict) and verify.get("id") == user.metabase_user_id:
                gids = ensure_metabase_groups(metabase_url, project_root, session)
                if gids is not None:
                    _apply_role(metabase_url, session, user.metabase_user_id, user.role, gids)
                return user.metabase_user_id

        password = generate_metabase_password()
        mb_user = _mb_post(
            metabase_url,
            session,
            "/api/user",
            {
                "first_name": user.email.split("@")[0],
                "last_name": ".",
                "email": user.email,
                "password": password,
            },
        )
        if mb_user is None:
            return None

        mb_user_id: int = mb_user["id"]
        encrypted_pw = encrypt_metabase_password(password, project_root)
        update_user(
            db_path,
            user_id,
            UserUpdate(metabase_user_id=mb_user_id, metabase_password_enc=encrypted_pw),
        )

        gids = ensure_metabase_groups(metabase_url, project_root, session)
        if gids is not None:
            _apply_role(metabase_url, session, mb_user_id, user.role, gids)

        logger.info("Created Metabase user %s for %s", mb_user_id, user.email)
        return mb_user_id

    except Exception:
        logger.exception("Failed to sync user %s to Metabase", user_id)
        return None


def sync_user_role(db_path: Path, user_id: str, project_root: Path, metabase_url: str) -> bool:
    """Update a Metabase user's groups after a Dango role change."""
    try:
        user = get_user_by_id(db_path, user_id)
        if user is None:
            logger.error("User %s not found in auth.db", user_id)
            return False

        if user.metabase_user_id is None:
            if sync_user_to_metabase(db_path, user_id, project_root, metabase_url) is None:
                return False
            user = get_user_by_id(db_path, user_id)
            if user is None or user.metabase_user_id is None:
                return False

        session = _get_admin_session(metabase_url, project_root)
        if session is None:
            return False
        gids = ensure_metabase_groups(metabase_url, project_root, session)
        if gids is None:
            return False

        _apply_role(metabase_url, session, user.metabase_user_id, user.role, gids)
        logger.info(
            "Synced role %s for Metabase user %s",
            user.role.value,
            user.metabase_user_id,
        )
        return True

    except Exception:
        logger.exception("Failed to sync role for user %s", user_id)
        return False


def deactivate_metabase_user(
    db_path: Path, user_id: str, project_root: Path, metabase_url: str
) -> bool:
    """Deactivate a user's Metabase account (soft-delete via DELETE)."""
    try:
        user = get_user_by_id(db_path, user_id)
        if user is None or user.metabase_user_id is None:
            return False
        session = _get_admin_session(metabase_url, project_root)
        if session is None:
            return False
        if _mb_delete(metabase_url, session, f"/api/user/{user.metabase_user_id}"):
            logger.info("Deactivated Metabase user %s", user.metabase_user_id)
            return True
        return False
    except Exception:
        logger.exception("Failed to deactivate Metabase user for %s", user_id)
        return False


def delete_metabase_user(
    db_path: Path, user_id: str, project_root: Path, metabase_url: str
) -> bool:
    """Deactivate a Metabase user and clear ``metabase_user_id`` in auth.db."""
    if not deactivate_metabase_user(db_path, user_id, project_root, metabase_url):
        return False
    try:
        update_user(
            db_path,
            user_id,
            UserUpdate(metabase_user_id=None, metabase_password_enc=None),
        )
        return True
    except Exception:
        logger.exception("Failed to clear metabase fields for user %s", user_id)
        return False


def ensure_duckdb_readonly(project_root: Path, metabase_url: str) -> bool:
    """Set the DuckDB database to read-only mode in Metabase."""
    try:
        session = _get_admin_session(metabase_url, project_root)
        if session is None:
            return False
        db_id = _get_database_id(project_root)
        if db_id is None:
            return False
        db_resp = _mb_get(metabase_url, session, f"/api/database/{db_id}")
        if not isinstance(db_resp, dict):
            return False
        details = db_resp.get("details", {})
        if not isinstance(details, dict):
            details = {}
        details["read_only"] = True
        db_resp["details"] = details
        result = _mb_put(metabase_url, session, f"/api/database/{db_id}", db_resp)
        if result is not None:
            logger.info("Set DuckDB database %s to read-only", db_id)
            return True
        return False
    except Exception:
        logger.exception("Failed to set DuckDB read-only mode")
        return False


def sync_all_users_to_metabase(
    db_path: Path, project_root: Path, metabase_url: str
) -> dict[str, Any]:
    """Full reconciliation: sync all active Dango users to Metabase."""
    result: dict[str, Any] = {
        "synced": 0,
        "created": 0,
        "warnings": [],
        "errors": [],
    }
    warnings: list[str] = result["warnings"]
    errors: list[str] = result["errors"]

    try:
        session = _get_admin_session(metabase_url, project_root)
        if session is None:
            errors.append("Failed to authenticate with Metabase")
            return result
        gids = ensure_metabase_groups(metabase_url, project_root, session)
        if gids is None:
            errors.append("Failed to ensure Metabase groups")
            return result
        ensure_duckdb_readonly(project_root, metabase_url)

        dango_users = list_users(db_path, active_only=True)
        mb_resp = _mb_get(metabase_url, session, "/api/user?limit=2000")
        mb_by_email: dict[str, dict[str, Any]] = {}
        if isinstance(mb_resp, list):
            mb_by_email = {u.get("email", ""): u for u in mb_resp}
        elif isinstance(mb_resp, dict):
            mb_by_email = {u.get("email", ""): u for u in mb_resp.get("data", [])}

        dango_emails: set[str] = set()
        for user in dango_users:
            dango_emails.add(user.email)
            try:
                if user.metabase_user_id is not None:
                    mb = mb_by_email.get(user.email)
                    if mb and mb.get("id") == user.metabase_user_id:
                        _apply_role(
                            metabase_url,
                            session,
                            user.metabase_user_id,
                            user.role,
                            gids,
                        )
                        result["synced"] += 1
                        continue
                mb_id = sync_user_to_metabase(db_path, user.id, project_root, metabase_url)
                if mb_id is not None:
                    result["created"] += 1
                else:
                    errors.append(f"Failed to create Metabase user for {user.email}")
            except Exception:
                logger.exception("Error syncing user %s", user.email)
                errors.append(f"Error syncing {user.email}")

        for email, mb_u in mb_by_email.items():
            if (
                email != "admin@dango.local"
                and email not in dango_emails
                and mb_u.get("is_active", True)
            ):
                warnings.append(f"Metabase user '{email}' not found in Dango")

    except Exception:
        logger.exception("Full Metabase sync failed")
        errors.append("Unexpected error during full sync")

    return result
