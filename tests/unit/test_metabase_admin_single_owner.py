"""tests/unit/test_metabase_admin_single_owner.py

The Metabase admin account's password has one owner (migration/link). These tests cover
is_metabase_admin_email and sync_user_to_metabase's admin and unapplied-password rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.auth.database import create_user, get_user_by_id
from dango.auth.metabase_sync import decrypt_metabase_password, encrypt_metabase_password
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner
from dango.security.metabase_config import is_metabase_admin_email

MB_URL = "http://localhost:3000"
ADMIN_EMAIL = "admin@test.com"
SYNC = "dango.auth.metabase_sync"


def _project(tmp_path: Path, *, metadata: dict[str, Any] | None = None) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    if metadata is not None:
        (dango_dir / "metabase.yml").write_text(yaml.safe_dump(metadata))
    return tmp_path


def _admin_metadata() -> dict[str, Any]:
    return {"metabase_url": MB_URL, "admin": {"email": ADMIN_EMAIL, "password": "yml-pw"}}


def _db(project_root: Path) -> Path:
    db_path = project_root / ".dango" / "auth.db"
    mig = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=mig).apply_pending()
    return db_path


def _user(db_path: Path, email: str, **kw: Any) -> User:
    user = User(email=email, password_hash="$2b$12$fake", role=Role.VIEWER, **kw)
    create_user(db_path, user)
    return user


@pytest.mark.unit
def test_is_metabase_admin_email_matches_case_insensitively(tmp_path: Path) -> None:
    root = _project(tmp_path, metadata=_admin_metadata())
    assert is_metabase_admin_email(root, "ADMIN@Test.com ")
    assert not is_metabase_admin_email(root, "other@test.com")


@pytest.mark.unit
def test_is_metabase_admin_email_false_without_metadata_or_email(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert not is_metabase_admin_email(root, ADMIN_EMAIL)
    (root / ".dango" / "metabase.yml").write_text(yaml.safe_dump({"metabase_url": MB_URL}))
    assert not is_metabase_admin_email(root, ADMIN_EMAIL)


def _run_sync(
    db_path: Path,
    root: Path,
    user_id: str,
    *,
    update_ok: bool = True,
    credentials: tuple[str, str] | None = (ADMIN_EMAIL, "working-admin-pw"),
) -> tuple[int | None, MagicMock]:
    from dango.auth.metabase_sync import sync_user_to_metabase

    with (
        patch(f"{SYNC}._get_admin_session", return_value="sess"),
        patch(f"{SYNC}._mb_post", return_value=None),
        patch(f"{SYNC}.find_metabase_user_by_email", return_value={"id": 7}),
        patch(f"{SYNC}.update_metabase_user_password", return_value=update_ok) as update_pw,
        patch(f"{SYNC}.ensure_metabase_groups", return_value=None),
        patch(f"{SYNC}.load_metabase_admin_credentials", return_value=credentials),
    ):
        return sync_user_to_metabase(db_path, user_id, root, MB_URL), update_pw


@pytest.mark.unit
def test_sync_links_admin_with_the_working_admin_credential_and_never_rotates(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, metadata=_admin_metadata())
    db_path = _db(root)
    user = _user(db_path, ADMIN_EMAIL)

    result, update_pw = _run_sync(db_path, root, user.id)

    assert result == 7
    update_pw.assert_not_called()
    stored = get_user_by_id(db_path, user.id)
    assert stored is not None and stored.metabase_user_id == 7
    assert stored.metabase_password_enc is not None
    assert decrypt_metabase_password(stored.metabase_password_enc, root) == "working-admin-pw"


@pytest.mark.unit
def test_sync_returns_none_for_admin_without_a_credential(tmp_path: Path) -> None:
    root = _project(tmp_path, metadata=_admin_metadata())
    db_path = _db(root)
    user = _user(db_path, ADMIN_EMAIL)

    result, update_pw = _run_sync(db_path, root, user.id, credentials=None)

    assert result is None
    update_pw.assert_not_called()
    stored = get_user_by_id(db_path, user.id)
    assert stored is not None
    assert stored.metabase_user_id is None and stored.metabase_password_enc is None


@pytest.mark.unit
def test_sync_non_admin_existing_user_gets_applied_password(tmp_path: Path) -> None:
    root = _project(tmp_path, metadata=_admin_metadata())
    db_path = _db(root)
    user = _user(db_path, "member@test.com")

    result, update_pw = _run_sync(db_path, root, user.id)

    assert result == 7
    update_pw.assert_called_once()
    applied = update_pw.call_args.args[3]
    stored = get_user_by_id(db_path, user.id)
    assert stored is not None and stored.metabase_password_enc is not None
    assert decrypt_metabase_password(stored.metabase_password_enc, root) == applied


@pytest.mark.unit
def test_sync_non_admin_does_not_store_unapplied_password(tmp_path: Path) -> None:
    root = _project(tmp_path, metadata=_admin_metadata())
    db_path = _db(root)
    old_enc = encrypt_metabase_password("previous-pw", root)
    user = _user(db_path, "member@test.com", metabase_password_enc=old_enc)

    result, _ = _run_sync(db_path, root, user.id, update_ok=False)

    assert result is None
    stored = get_user_by_id(db_path, user.id)
    assert stored is not None
    assert stored.metabase_password_enc == old_enc
    assert stored.metabase_user_id is None
