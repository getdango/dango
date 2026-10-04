"""dango/platform/common/metabase_credential_migration.py

Crash-safe migration of legacy project-local Metabase administrator passwords.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from dango.security.metabase_config import (
    load_metabase_metadata,
    write_metabase_metadata,
)
from dango.security.metabase_credentials import MetabaseCredentialStore

_STATE_VERSION = 1
_STATE_FILENAME = "metabase_credential_migration.json"
_LOCK_FILENAME = "metabase_credential_migration.lock"


def prepare_metabase_credential_migration(project_root: Path) -> dict[str, object]:
    """Record that a legacy credential needs an online completion step.

    This function deliberately performs no project-identity lookup, protected
    store access, password generation, or HTTP request.  ``dango upgrade`` can
    therefore call it safely while an old project is stopped.
    """
    root = Path(project_root)
    with _migration_lock(root):
        metadata = load_metabase_metadata(root)
        if not _legacy_password(metadata):
            return {"status": "not_required"}
        state = {"version": _STATE_VERSION, "status": "prepared"}
        _write_state(root, state)
        return state


def migration_pending(project_root: Path) -> bool:
    """True while a legacy project-local Metabase admin password still needs migrating.

    Read-only: loads ``.dango/metabase.yml`` and makes no HTTP request.
    """
    try:
        return _legacy_password(load_metabase_metadata(Path(project_root))) is not None
    except Exception:
        return False


def complete_metabase_credential_migration(project_root: Path) -> dict[str, object]:
    """Move a legacy credential into protected storage after Metabase is ready.

    The only durable password candidates are the legacy YAML value and the
    protected store's pending/active slots.  The state file records phase only,
    so an interrupted process can retry without revealing a credential or
    rotating twice.
    """
    root = Path(project_root)
    with _migration_lock(root):
        metadata = load_metabase_metadata(root)
        legacy_password = _legacy_password(metadata)
        if metadata is None or legacy_password is None:
            return {"status": "not_required"}

        admin = metadata.get("admin")
        if not isinstance(admin, dict) or not isinstance(admin.get("email"), str):
            return _failed(root, "missing_admin_email")
        admin_email = admin["email"]
        metabase_url = metadata.get("metabase_url", "http://localhost:3000")
        if not isinstance(metabase_url, str) or not metabase_url:
            return _failed(root, "missing_metabase_url")
        metabase_url = metabase_url.rstrip("/")

        try:
            store = MetabaseCredentialStore(root)
            active_password = store.load()
        except Exception:
            return _failed(root, "protected_store_unavailable")

        # A protected active credential can only have been written by a prior
        # verified transition.  Re-authenticate it before removing the legacy
        # copy; do not rotate again merely because cleanup was interrupted.
        current_password = active_password or legacy_password
        current_session = _create_session(metabase_url, admin_email, current_password)

        try:
            pending_password = store.load_pending()
        except Exception:
            return _failed(root, "pending_store_unavailable")

        if pending_password is not None:
            pending_session = _create_session(metabase_url, admin_email, pending_password)
            if pending_session is not None:
                # A timeout or interruption after the remote change is a
                # recovery case, not permission to make a second change.
                try:
                    active_password = store.promote_pending()
                except Exception:
                    return _failed(root, "pending_promotion_failed")
                _write_state(root, {"version": _STATE_VERSION, "status": "promoted"})
                return _refresh_sso_and_cleanup(
                    root, metadata, metabase_url, pending_session, admin_email, active_password
                )
            if current_session is None:
                # The remote API may have accepted the candidate before its
                # response was lost. Retain that protected candidate for a
                # later retry rather than discarding the only recoverable
                # credential after both login attempts fail.
                return _failed(root, "credential_recovery_pending")
            # The current credential is known good, so this candidate never
            # reached Metabase and can safely be discarded before retrying.
            try:
                store.discard_pending()
            except Exception:
                return _failed(root, "stale_pending_cleanup_failed")

        if current_session is None:
            return _failed(root, "current_credential_not_accepted")

        if active_password is not None:
            _write_state(root, {"version": _STATE_VERSION, "status": "promoted"})
            return _refresh_sso_and_cleanup(
                root, metadata, metabase_url, current_session, admin_email, active_password
            )

        from dango.auth.metabase_sync import (
            generate_metabase_password,
            update_metabase_user_password,
        )

        candidate = generate_metabase_password()
        try:
            store.save_pending(candidate)
        except Exception:
            return _failed(root, "candidate_staging_failed")
        _write_state(root, {"version": _STATE_VERSION, "status": "candidate_staged"})

        metabase_user = _find_metabase_user(metabase_url, current_session, admin_email)
        if metabase_user is None:
            return _failed(root, "metabase_admin_user_not_found")

        update_succeeded = update_metabase_user_password(
            metabase_url,
            current_session,
            metabase_user["id"],
            candidate,
            old_password=legacy_password,
        )
        candidate_session = _create_session(metabase_url, admin_email, candidate)
        if candidate_session is None:
            if not update_succeeded:
                try:
                    store.discard_pending()
                except Exception:
                    pass
                return _failed(root, "remote_update_failed")
            # The remote result was successful but verification was not. Keep
            # the protected candidate for a later verified recovery attempt.
            return _failed(root, "candidate_verification_pending")

        _write_state(root, {"version": _STATE_VERSION, "status": "candidate_verified"})
        try:
            active_password = store.promote_pending()
        except Exception:
            return _failed(root, "pending_promotion_failed")
        _write_state(root, {"version": _STATE_VERSION, "status": "promoted"})
        return _refresh_sso_and_cleanup(
            root, metadata, metabase_url, candidate_session, admin_email, active_password
        )


def _refresh_sso_and_cleanup(
    project_root: Path,
    metadata: dict[str, Any],
    metabase_url: str,
    session: str,
    admin_email: str,
    password: str,
) -> dict[str, object]:
    """Refresh existing linked SSO records, then remove only YAML password."""
    metabase_user = _find_metabase_user(metabase_url, session, admin_email)
    if metabase_user is None:
        return _failed(project_root, "metabase_admin_user_not_found")
    user_id = metabase_user.get("id")
    if not isinstance(user_id, int):
        return _failed(project_root, "invalid_metabase_admin_user")

    try:
        _refresh_linked_sso_passwords(project_root, user_id, password)
    except Exception:
        return _failed(project_root, "sso_refresh_failed")

    cleaned_metadata = dict(metadata)
    admin = cleaned_metadata.get("admin")
    if isinstance(admin, dict):
        cleaned_metadata["admin"] = {
            key: value for key, value in admin.items() if key != "password"
        }
    try:
        write_metabase_metadata(project_root, cleaned_metadata)
    except Exception:
        return _failed(project_root, "metadata_cleanup_failed")
    state = {"version": _STATE_VERSION, "status": "secure_rotated"}
    _write_state(project_root, state)
    return state


def _refresh_linked_sso_passwords(project_root: Path, metabase_user_id: int, password: str) -> None:
    """Update encrypted SSO credentials for Dango users already linked to root."""
    from dango.auth.admin import get_auth_db_path
    from dango.auth.database import list_users, update_user
    from dango.auth.metabase_sync import encrypt_metabase_password
    from dango.auth.models import UserUpdate

    db_path = get_auth_db_path(project_root)
    if not db_path.exists():
        return
    encrypted_password = encrypt_metabase_password(password, project_root)
    for user in list_users(db_path, active_only=False):
        if user.metabase_user_id == metabase_user_id:
            update_user(db_path, user.id, UserUpdate(metabase_password_enc=encrypted_password))


def _find_metabase_user(metabase_url: str, session: str, email: str) -> dict[str, Any] | None:
    """Find the Metabase root user without retaining its API response in state."""
    from dango.auth.metabase_sync import find_metabase_user_by_email

    user = find_metabase_user_by_email(metabase_url, session, email)
    return user if isinstance(user, dict) else None


def _create_session(metabase_url: str, email: str, password: str) -> str | None:
    """Authenticate one password candidate without exposing response details."""
    import requests

    try:
        response = requests.post(
            f"{metabase_url}/api/session",
            json={"username": email, "password": password},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:
        return None
    session = payload.get("id") if isinstance(payload, dict) else None
    return session if isinstance(session, str) and session else None


def _legacy_password(metadata: dict[str, Any] | None) -> str | None:
    """Return the legacy password only for the duration of this transition."""
    if not isinstance(metadata, dict):
        return None
    admin = metadata.get("admin")
    password = admin.get("password") if isinstance(admin, dict) else None
    return password if isinstance(password, str) and password else None


def _state_path(project_root: Path) -> Path:
    return Path(project_root) / ".dango" / "state" / _STATE_FILENAME


def _write_state(project_root: Path, state: dict[str, object]) -> None:
    """Atomically persist phase-only migration state with owner-only mode."""
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            fd = None
            json.dump(state, state_file, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def _failed(project_root: Path, reason: str) -> dict[str, object]:
    """Record an error-safe, retryable failure without its sensitive cause."""
    state = {"version": _STATE_VERSION, "status": "failed_non_destructive", "reason": reason}
    _write_state(project_root, state)
    return state


@contextmanager
def _migration_lock(project_root: Path) -> Generator[None, None, None]:
    """Hold the dedicated migration lock without deleting its inode on exit."""
    state_dir = Path(project_root) / ".dango" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / _LOCK_FILENAME
    lock_file = lock_path.open("a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
