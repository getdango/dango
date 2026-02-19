"""tests/unit/test_metabase_sync.py

Tests for Metabase user sync in dango/auth/metabase_sync.py.
All Metabase API calls are mocked via ``@patch``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.auth.database import create_user, get_user_by_id, update_user
from dango.auth.models import Role, User, UserUpdate
from dango.migrations.runner import MigrationRunner

MB_URL = "http://localhost:3000"


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "auth.db"
    mig_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=mig_dir).apply_pending()
    return db_path


def _user_in_db(db: Path, **kw: Any) -> User:
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": "$2b$12$fake",
        "role": Role.VIEWER,
    }
    defaults.update(kw)
    user = User(**defaults)
    create_user(db, user)
    return user


def _mb_yml(tmp_path: Path, db_id: int = 1) -> Path:
    d = tmp_path / ".dango"
    d.mkdir(exist_ok=True)
    (d / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": MB_URL,
                "admin": {"email": "admin@dango.local", "password": "dangolocal123"},
                "database": {"id": db_id, "name": "Test Analytics"},
            }
        )
    )
    return tmp_path


def _resp(status: int = 200, data: Any = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data if data is not None else {}
    return r


# ---- Groups list used across tests ----
_GROUPS = [{"id": 1, "name": "All Users"}, {"id": 2, "name": "Administrators"}]
_GROUPS_WITH_EDITORS = [*_GROUPS, {"id": 5, "name": "Dango Editors"}]


@pytest.mark.unit
class TestMetabaseCredentials:
    """Tests for credential loading and admin session."""

    def test_load_missing_file(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import _load_metabase_credentials

        assert _load_metabase_credentials(tmp_path) is None

    def test_load_valid_file(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import _load_metabase_credentials

        assert _load_metabase_credentials(_mb_yml(tmp_path)) is not None

    @patch("dango.auth.metabase_sync.requests")
    def test_admin_session_success(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import _get_admin_session

        mock_req.post.return_value = _resp(200, {"id": "tok"})
        assert _get_admin_session(MB_URL, _mb_yml(tmp_path)) == "tok"

    @patch("dango.auth.metabase_sync.requests")
    def test_admin_session_failure(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import _get_admin_session

        mock_req.post.return_value = _resp(401)
        assert _get_admin_session(MB_URL, _mb_yml(tmp_path)) is None


@pytest.mark.unit
class TestMetabasePassword:
    """Tests for password generation, encryption, and decryption."""

    def test_generate_length(self) -> None:
        from dango.auth.metabase_sync import generate_metabase_password

        assert len(generate_metabase_password()) >= 32

    def test_generate_uniqueness(self) -> None:
        from dango.auth.metabase_sync import generate_metabase_password

        assert len({generate_metabase_password() for _ in range(10)}) == 10

    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    def test_encrypt_decrypt(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import decrypt_metabase_password, encrypt_metabase_password

        inst = MagicMock()
        mock_cls.return_value = inst
        inst.encrypt_token.return_value = "enc-blob"
        inst.decrypt_token.return_value = {"metabase_password": "pw"}

        assert encrypt_metabase_password("pw", tmp_path) == "enc-blob"
        assert decrypt_metabase_password("enc-blob", tmp_path) == "pw"


@pytest.mark.unit
class TestSyncUserToMetabase:
    """Tests for sync_user_to_metabase."""

    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    @patch("dango.auth.metabase_sync.requests")
    def test_creates_new_user(
        self, mock_req: MagicMock, mock_sts: MagicMock, tmp_path: Path
    ) -> None:
        from dango.auth.metabase_sync import sync_user_to_metabase

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, email="alice@example.com")

        mock_sts.return_value.encrypt_token.return_value = "enc-pw"
        mock_req.post.side_effect = [
            _resp(200, {"id": "s"}),
            _resp(201, {"id": 42}),
            _resp(200, {"id": "s"}),
            _resp(200, {"id": "s"}),
        ]
        mock_req.get.side_effect = [
            _resp(200, _GROUPS),
            _resp(200, {"groups": {}}),
            _resp(200, {"id": 42, "group_ids": [1]}),
        ]
        mock_req.put.return_value = _resp(200, {"groups": {}})

        assert sync_user_to_metabase(db, user.id, root, MB_URL) == 42
        u = get_user_by_id(db, user.id)
        assert u is not None and u.metabase_user_id == 42 and u.metabase_password_enc == "enc-pw"

    @patch("dango.auth.metabase_sync.requests")
    def test_existing_user_verified(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_to_metabase

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, email="bob@example.com", metabase_user_id=99)

        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.get.side_effect = [
            _resp(200, {"id": 99}),
            _resp(200, _GROUPS),
            _resp(200, {"groups": {}}),
            _resp(200, {"id": 99, "group_ids": [1]}),
        ]
        mock_req.put.return_value = _resp(200, {"groups": {}})
        assert sync_user_to_metabase(db, user.id, root, MB_URL) == 99

    @patch("dango.auth.metabase_sync.requests")
    def test_api_failure_returns_none(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_to_metabase

        db = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _user_in_db(db)
        mock_req.post.return_value = _resp(500)
        assert sync_user_to_metabase(db, user.id, tmp_path, MB_URL) is None

    def test_user_not_found(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_to_metabase

        assert sync_user_to_metabase(_make_db(tmp_path), "ghost", tmp_path, MB_URL) is None


@pytest.mark.unit
class TestSyncUserRole:
    """Tests for sync_user_role."""

    @patch("dango.auth.metabase_sync.requests")
    def test_updates_role_groups(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_role

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, email="ed@example.com", role=Role.EDITOR, metabase_user_id=10)

        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.get.side_effect = [
            _resp(200, _GROUPS_WITH_EDITORS),
            _resp(200, {"groups": {}}),
            _resp(200, {"id": 10, "group_ids": [1]}),
        ]
        mock_req.put.return_value = _resp(200, {})
        assert sync_user_role(db, user.id, root, MB_URL) is True

    @patch("dango.auth.metabase_sync.requests")
    def test_demotion_removes_group(self, mock_req: MagicMock, tmp_path: Path) -> None:
        """Editor→Viewer demotion removes editors group via membership lookup."""
        from dango.auth.metabase_sync import sync_user_role

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        # User is Viewer but still in editors group (5) in Metabase
        user = _user_in_db(db, email="demoted@example.com", role=Role.VIEWER, metabase_user_id=10)

        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.get.side_effect = [
            _resp(200, _GROUPS_WITH_EDITORS),  # ensure_groups: list groups
            _resp(200, {"groups": {}}),  # ensure_groups: permissions graph
            _resp(200, {"id": 10, "group_ids": [1, 5]}),  # _sync: user detail
            # _sync: lookup group 5 members to find membership_id
            _resp(200, {"members": [{"user_id": 10, "membership_id": 77}]}),
        ]
        mock_req.put.return_value = _resp(200, {})
        mock_req.delete.return_value = _resp(204)
        assert sync_user_role(db, user.id, root, MB_URL) is True
        # Verify DELETE was called for membership 77
        delete_calls = list(mock_req.delete.call_args_list)
        assert any("/api/permissions/membership/77" in str(c) for c in delete_calls)

    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    @patch("dango.auth.metabase_sync.requests")
    def test_creates_user_if_missing(
        self, mock_req: MagicMock, mock_sts: MagicMock, tmp_path: Path
    ) -> None:
        from dango.auth.metabase_sync import sync_user_role

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, email="new@example.com")
        mock_sts.return_value.encrypt_token.return_value = "enc"

        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.post.side_effect = [
            _resp(200, {"id": "s"}),
            _resp(201, {"id": 50}),
            *[_resp(200, {"id": "s"}) for _ in range(5)],
        ]
        mock_req.get.side_effect = [
            _resp(200, _GROUPS),
            _resp(200, {"groups": {}}),
            _resp(200, {"id": 50, "group_ids": [1]}),
            _resp(200, _GROUPS),
            _resp(200, {"groups": {}}),
            _resp(200, {"id": 50, "group_ids": [1]}),
        ]
        mock_req.put.return_value = _resp(200, {})
        assert sync_user_role(db, user.id, root, MB_URL) is True
        assert get_user_by_id(db, user.id) is not None


@pytest.mark.unit
class TestDeactivateAndDelete:
    """Tests for deactivate_metabase_user and delete_metabase_user."""

    @patch("dango.auth.metabase_sync.requests")
    def test_deactivate(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import deactivate_metabase_user

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, metabase_user_id=20)
        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.delete.return_value = _resp(204)
        assert deactivate_metabase_user(db, user.id, root, MB_URL) is True

    def test_deactivate_no_mb_id(self, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import deactivate_metabase_user

        db = _make_db(tmp_path)
        user = _user_in_db(db)
        assert deactivate_metabase_user(db, user.id, tmp_path, MB_URL) is False

    @patch("dango.auth.metabase_sync.requests")
    def test_delete_clears_fields(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import delete_metabase_user

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        user = _user_in_db(db, metabase_user_id=30, metabase_password_enc="enc")
        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.delete.return_value = _resp(204)
        assert delete_metabase_user(db, user.id, root, MB_URL) is True
        u = get_user_by_id(db, user.id)
        assert u is not None and u.metabase_user_id is None and u.metabase_password_enc is None


@pytest.mark.unit
class TestEnsureMetabaseGroups:
    """Tests for ensure_metabase_groups."""

    @patch("dango.auth.metabase_sync.requests")
    def test_creates_editors_group(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import ensure_metabase_groups

        root = _mb_yml(tmp_path)
        mock_req.post.side_effect = [
            _resp(200, {"id": "s"}),
            _resp(200, {"id": 7, "name": "Dango Editors"}),
        ]
        mock_req.get.side_effect = [_resp(200, _GROUPS), _resp(200, {"groups": {}})]
        mock_req.put.return_value = _resp(200, {"groups": {}})
        result = ensure_metabase_groups(MB_URL, root)
        assert result is not None
        assert result == {"editors": 7, "admin": 2, "all_users": 1}

    @patch("dango.auth.metabase_sync.requests")
    def test_reuses_existing_group(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import ensure_metabase_groups

        root = _mb_yml(tmp_path)
        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.get.side_effect = [_resp(200, _GROUPS_WITH_EDITORS), _resp(200, {"groups": {}})]
        mock_req.put.return_value = _resp(200, {"groups": {}})
        result = ensure_metabase_groups(MB_URL, root)
        assert result is not None and result["editors"] == 5
        assert mock_req.post.call_count == 1  # only login, no group create


@pytest.mark.unit
class TestEnsureDuckdbReadonly:
    """Tests for ensure_duckdb_readonly."""

    @patch("dango.auth.metabase_sync.requests")
    def test_sets_read_only(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import ensure_duckdb_readonly

        root = _mb_yml(tmp_path, db_id=3)
        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.get.return_value = _resp(200, {"id": 3, "details": {"path": "/data/w.duckdb"}})
        mock_req.put.return_value = _resp(200, {"id": 3})
        assert ensure_duckdb_readonly(root, MB_URL) is True
        body = mock_req.put.call_args.kwargs.get("json") or mock_req.put.call_args[1].get("json")
        assert body["details"]["read_only"] is True


@pytest.mark.unit
class TestSyncAllUsers:
    """Tests for sync_all_users_to_metabase."""

    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    @patch("dango.auth.metabase_sync.requests")
    def test_full_reconciliation(
        self, mock_req: MagicMock, mock_sts: MagicMock, tmp_path: Path
    ) -> None:
        from dango.auth.metabase_sync import sync_all_users_to_metabase

        db = _make_db(tmp_path)
        root = _mb_yml(tmp_path)
        _user_in_db(db, email="synced@example.com", metabase_user_id=10)
        _user_in_db(db, email="new@example.com")
        mock_sts.return_value.encrypt_token.return_value = "enc"

        mb_users = [
            {"id": 10, "email": "synced@example.com", "is_active": True},
            {"id": 1, "email": "admin@dango.local", "is_active": True},
            {"id": 99, "email": "orphan@example.com", "is_active": True},
        ]

        def mock_get(*a: Any, **kw: Any) -> MagicMock:
            url = a[0] if a else kw.get("url", "")
            if "/api/permissions/group" in url:
                return _resp(200, _GROUPS_WITH_EDITORS)
            if "/api/permissions/graph" in url:
                return _resp(200, {"groups": {}})
            if "/api/database/" in url:
                return _resp(200, {"id": 1, "details": {}})
            if "/api/user/" in url:
                return _resp(200, {"id": 10, "group_ids": [1]})
            if "/api/user" in url:
                return _resp(200, mb_users)
            return _resp(200, {})

        def mock_post(*a: Any, **kw: Any) -> MagicMock:
            url = a[0] if a else kw.get("url", "")
            if "/api/user" in url and "/api/session" not in url and "/api/permissions" not in url:
                return _resp(201, {"id": 55})
            return _resp(200, {"id": "s"})

        mock_req.get.side_effect = mock_get
        mock_req.post.side_effect = mock_post
        mock_req.put.return_value = _resp(200, {})

        result = sync_all_users_to_metabase(db, root, MB_URL)
        assert result["synced"] >= 1 and result["created"] >= 1
        assert any("orphan" in w for w in result["warnings"])
        assert not any("admin@dango.local" in w for w in result["warnings"])

    @patch("dango.auth.metabase_sync.requests")
    def test_auth_failure(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_all_users_to_metabase

        db = _make_db(tmp_path)
        _mb_yml(tmp_path)
        mock_req.post.return_value = _resp(401)
        result = sync_all_users_to_metabase(db, tmp_path, MB_URL)
        assert len(result["errors"]) > 0


@pytest.mark.unit
class TestErrorHandling:
    """Tests for graceful error handling on Metabase failures."""

    @patch("dango.auth.metabase_sync.requests")
    def test_connection_error(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_to_metabase

        db = _make_db(tmp_path)
        _mb_yml(tmp_path)
        import requests as real_requests

        mock_req.post.side_effect = real_requests.ConnectionError("refused")
        assert sync_user_to_metabase(db, _user_in_db(db).id, tmp_path, MB_URL) is None

    @patch("dango.auth.metabase_sync.requests")
    def test_500_returns_false(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import deactivate_metabase_user

        db = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _user_in_db(db, metabase_user_id=10)
        mock_req.post.return_value = _resp(200, {"id": "s"})
        mock_req.delete.return_value = _resp(500)
        assert deactivate_metabase_user(db, user.id, tmp_path, MB_URL) is False

    @patch("dango.auth.metabase_sync.requests")
    def test_role_sync_graceful(self, mock_req: MagicMock, tmp_path: Path) -> None:
        from dango.auth.metabase_sync import sync_user_role

        db = _make_db(tmp_path)
        _mb_yml(tmp_path)
        user = _user_in_db(db, metabase_user_id=10)
        mock_req.post.return_value = _resp(403)
        assert sync_user_role(db, user.id, tmp_path, MB_URL) is False


@pytest.mark.unit
class TestMigration003:
    """Verify migration 003 adds metabase_password_enc column."""

    def test_column_exists(self, tmp_path: Path) -> None:
        import sqlite3

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        conn.close()
        assert "metabase_password_enc" in cols

    def test_round_trip(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _user_in_db(db, email="enc@example.com", metabase_password_enc="blob")
        assert get_user_by_id(db, user.id) is not None
        assert get_user_by_id(db, user.id).metabase_password_enc == "blob"  # type: ignore[union-attr]

    def test_update(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        user = _user_in_db(db, email="upd@example.com")
        updated = update_user(db, user.id, UserUpdate(metabase_password_enc="new-enc"))
        assert updated.metabase_password_enc == "new-enc"
