"""dango/platform/common/metabase_link.py

Recoverable Metabase SSO administrator-linking state machine used by startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def link_metabase_admin(project_root: Path, admin_email: str) -> None:
    """Link the Metabase administrator to a Dango user without YAML secrets."""
    try:
        import requests

        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import get_user_by_email
        from dango.auth.metabase_sync import (
            find_metabase_user_by_email,
            generate_metabase_password,
            update_metabase_user_password,
        )
        from dango.logging import get_logger
        from dango.security.metabase_config import (
            load_metabase_admin_credentials,
            load_metabase_metadata,
        )
        from dango.security.metabase_credentials import MetabaseCredentialStore

        logger = get_logger(__name__)
        metadata = load_metabase_metadata(project_root)
        if metadata is None:
            return
        current_credentials = load_metabase_admin_credentials(project_root)
        if current_credentials is None:
            return
        mb_email, current_password = current_credentials
        mb_url = metadata.get("metabase_url", "http://localhost:3000").rstrip("/")

        def create_session(password: str) -> str | None:
            """Authenticate one candidate password and return its session token."""
            response = requests.post(
                f"{mb_url}/api/session",
                json={"username": mb_email, "password": password},
                timeout=10,
            )
            if response.status_code != 200:
                return None
            session = response.json().get("id")
            return session if session else None

        current_session = create_session(current_password)
        credential_store = MetabaseCredentialStore(project_root)
        pending_password = credential_store.load_pending()

        session_token: str | None = None
        active_password: str | None = None
        recovered_pending = False
        if pending_password is not None:
            pending_session = create_session(pending_password)
            if pending_session is not None:
                credential_store.promote_pending()
                metadata = _write_password_free_metadata(project_root, metadata)
                session_token = pending_session
                active_password = pending_password
                recovered_pending = True
            elif current_session is not None:
                credential_store.discard_pending()
                session_token = current_session
                active_password = current_password
            else:
                return
        elif current_session is not None:
            session_token = current_session
            active_password = current_password
        else:
            return

        if session_token is None or active_password is None:
            return

        db_path = get_auth_db_path(project_root)
        if not db_path.exists():
            return
        dango_user = get_user_by_email(db_path, admin_email)
        if dango_user is None:
            return
        if dango_user.metabase_user_id is not None:
            admin_metadata = metadata.get("admin")
            if isinstance(admin_metadata, dict) and "password" in admin_metadata:
                credential_store.save(active_password)
                _write_password_free_metadata(project_root, metadata)
            return

        mb_user = find_metabase_user_by_email(mb_url, session_token, admin_email)
        if mb_user is None:
            return
        if recovered_pending:
            _store_sso_password(
                project_root, db_path, dango_user.id, mb_user["id"], active_password
            )
            logger.info("metabase_admin_linked", email=admin_email, metabase_user_id=mb_user["id"])
            return

        password = generate_metabase_password()
        credential_store.save_pending(password)
        update_succeeded = update_metabase_user_password(
            mb_url,
            session_token,
            mb_user["id"],
            password,
            old_password=active_password,
        )
        candidate_session = create_session(password)
        if not update_succeeded and candidate_session is None:
            try:
                credential_store.discard_pending()
            except Exception:
                pass
            logger.warning("metabase_password_update_failed", metabase_user_id=mb_user["id"])
            return
        if candidate_session is None:
            logger.warning(
                "metabase_password_candidate_verification_failed",
                metabase_user_id=mb_user["id"],
            )
            return
        credential_store.promote_pending()
        _write_password_free_metadata(project_root, metadata)
        _store_sso_password(project_root, db_path, dango_user.id, mb_user["id"], password)
        logger.info("metabase_admin_linked", email=admin_email, metabase_user_id=mb_user["id"])
    except Exception:
        # Non-critical — SSO bridge will lazy-sync on next login.
        pass


def _write_password_free_metadata(project_root: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Persist metadata with only the legacy administrator password removed."""
    from dango.security.metabase_config import write_metabase_metadata

    cleaned_metadata = dict(metadata)
    admin_metadata = cleaned_metadata.get("admin")
    if isinstance(admin_metadata, dict):
        cleaned_metadata["admin"] = {
            key: value for key, value in admin_metadata.items() if key != "password"
        }
    write_metabase_metadata(project_root, cleaned_metadata)
    return cleaned_metadata


def _store_sso_password(
    project_root: Path,
    db_path: Path,
    dango_user_id: str,
    metabase_user_id: int,
    password: str,
) -> None:
    """Encrypt and store the Dango user's Metabase SSO credential."""
    from dango.auth.database import update_user
    from dango.auth.metabase_sync import encrypt_metabase_password
    from dango.auth.models import UserUpdate

    encrypted_password = encrypt_metabase_password(password, project_root)
    update_user(
        db_path,
        dango_user_id,
        UserUpdate(
            metabase_user_id=metabase_user_id,
            metabase_password_enc=encrypted_password,
        ),
    )
