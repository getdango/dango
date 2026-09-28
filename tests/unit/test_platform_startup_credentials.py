"""tests/unit/test_platform_startup_credentials.py

Focused credential-transition tests for the Metabase SSO startup link.
"""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.common.startup import _link_metabase_admin


def _session_response(session_id: str | None) -> MagicMock:
    """Build a Metabase session response with the requested result."""
    response = MagicMock(status_code=200 if session_id else 401)
    response.json.return_value = {"id": session_id} if session_id else {}
    return response


def _dependencies(tmp_path: Path) -> tuple[ExitStack, dict[str, MagicMock]]:
    """Mock the lazy imports used by the isolated SSO-link state machine."""
    auth_db = tmp_path / ".dango" / "auth.db"
    auth_db.parent.mkdir(exist_ok=True)
    auth_db.touch()
    metadata = {
        "metabase_url": "http://localhost:3000",
        "admin": {"email": "admin@test.com", "password": "legacy-password", "name": "Admin"},
        "database": {"id": 1},
    }
    store = MagicMock()
    user = SimpleNamespace(id="dango-user", metabase_user_id=None)
    patches = {
        "metadata": patch(
            "dango.security.metabase_config.load_metabase_metadata", return_value=metadata
        ),
        "credentials": patch(
            "dango.security.metabase_config.load_metabase_admin_credentials",
            return_value=("admin@test.com", "legacy-password"),
        ),
        "store_class": patch(
            "dango.security.metabase_credentials.MetabaseCredentialStore", return_value=store
        ),
        "write_metadata": patch("dango.security.metabase_config.write_metabase_metadata"),
        "get_auth_db": patch("dango.auth.admin.get_auth_db_path", return_value=auth_db),
        "get_user": patch("dango.auth.database.get_user_by_email", return_value=user),
        "update_user": patch("dango.auth.database.update_user"),
        "find_user": patch(
            "dango.auth.metabase_sync.find_metabase_user_by_email",
            return_value={"id": 42, "email": "admin@test.com"},
        ),
        "generate_password": patch(
            "dango.auth.metabase_sync.generate_metabase_password", return_value="candidate-password"
        ),
        "update_password": patch(
            "dango.auth.metabase_sync.update_metabase_user_password", return_value=True
        ),
        "encrypt_password": patch(
            "dango.auth.metabase_sync.encrypt_metabase_password", return_value="encrypted-candidate"
        ),
        "logger": patch("dango.logging.get_logger", return_value=MagicMock()),
        "post": patch("requests.post"),
    }
    stack = ExitStack()
    mocks = {name: stack.enter_context(patcher) for name, patcher in patches.items()}
    mocks["store"] = store
    return stack, mocks


@pytest.mark.unit
def test_link_stages_candidate_before_remote_password_update(tmp_path: Path) -> None:
    """A password candidate is protected before the remote mutation begins."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        events: list[str] = []
        mocks["store"].load_pending.return_value = None
        mocks["store"].save_pending.side_effect = lambda password: events.append("stage")
        mocks["update_password"].side_effect = lambda *args, **kwargs: (
            events.append("update") or True
        )
        mocks["post"].side_effect = [_session_response("current"), _session_response("candidate")]

        _link_metabase_admin(tmp_path, "admin@test.com")

        assert events == ["stage", "update"]
        mocks["store"].save_pending.assert_called_once_with("candidate-password")
        mocks["update_password"].assert_called_once_with(
            "http://localhost:3000",
            "current",
            42,
            "candidate-password",
            old_password="legacy-password",
        )


@pytest.mark.unit
def test_link_staging_failure_causes_no_remote_update_or_metadata_write(tmp_path: Path) -> None:
    """A candidate-store failure exits before any irreversible work occurs."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = None
        mocks["store"].save_pending.side_effect = RuntimeError("store unavailable")
        mocks["post"].return_value = _session_response("current")

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["update_password"].assert_not_called()
        mocks["write_metadata"].assert_not_called()


@pytest.mark.unit
def test_link_promotes_only_after_candidate_login_verifies(tmp_path: Path) -> None:
    """An unverified remote update leaves the pending secret and YAML unchanged."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = None
        mocks["post"].side_effect = [_session_response("current"), _session_response(None)]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["store"].promote_pending.assert_not_called()
        mocks["write_metadata"].assert_not_called()
        mocks["store"].discard_pending.assert_not_called()


@pytest.mark.unit
def test_link_recovers_an_ambiguous_remote_update_when_candidate_login_verifies(
    tmp_path: Path,
) -> None:
    """A timeout-like update result promotes a candidate that Metabase accepts."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = None
        mocks["update_password"].return_value = False
        mocks["post"].side_effect = [_session_response("current"), _session_response("candidate")]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["store"].promote_pending.assert_called_once_with()
        mocks["store"].discard_pending.assert_not_called()
        mocks["write_metadata"].assert_called_once()
        mocks["encrypt_password"].assert_called_once_with("candidate-password", tmp_path)


@pytest.mark.unit
def test_link_recovers_verified_pending_without_second_rotation(tmp_path: Path) -> None:
    """A verified pending password is promoted and linked without another rotation."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = "pending-password"
        mocks["post"].side_effect = [_session_response(None), _session_response("pending-session")]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["store"].promote_pending.assert_called_once_with()
        cleaned_metadata = mocks["write_metadata"].call_args.args[1]
        assert cleaned_metadata["admin"] == {"email": "admin@test.com", "name": "Admin"}
        mocks["update_password"].assert_not_called()
        mocks["encrypt_password"].assert_called_once_with("pending-password", tmp_path)


@pytest.mark.unit
def test_link_discards_unverified_pending_when_current_login_works(tmp_path: Path) -> None:
    """A stale pending candidate cannot block a verified current credential."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = "stale-pending"
        mocks["post"].side_effect = [
            _session_response("current"),
            _session_response(None),
            _session_response("candidate"),
        ]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["store"].discard_pending.assert_called_once_with()
        mocks["update_password"].assert_called_once()


@pytest.mark.unit
def test_link_retains_pending_when_neither_password_logs_in(tmp_path: Path) -> None:
    """An ambiguous credential state is preserved for a later safe recovery."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = "pending-password"
        mocks["post"].side_effect = [_session_response(None), _session_response(None)]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["store"].discard_pending.assert_not_called()
        mocks["store"].promote_pending.assert_not_called()
        mocks["write_metadata"].assert_not_called()
        mocks["update_password"].assert_not_called()


@pytest.mark.unit
def test_link_existing_linked_user_cleans_legacy_password_after_verified_login(
    tmp_path: Path,
) -> None:
    """An already-linked user gets YAML cleanup without another password rotation."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        events: list[str] = []
        mocks["store"].load_pending.return_value = None
        mocks["get_user"].return_value = SimpleNamespace(id="dango-user", metabase_user_id=42)
        mocks["post"].return_value = _session_response("current")
        mocks["store"].save.side_effect = lambda password: events.append("save")
        mocks["write_metadata"].side_effect = lambda *args: events.append("write")

        _link_metabase_admin(tmp_path, "admin@test.com")

        cleaned_metadata = mocks["write_metadata"].call_args.args[1]
        assert "password" not in cleaned_metadata["admin"]
        assert events == ["save", "write"]
        mocks["store"].save.assert_called_once_with("legacy-password")
        mocks["update_password"].assert_not_called()


@pytest.mark.unit
def test_link_existing_linked_user_keeps_legacy_yaml_when_store_save_fails(tmp_path: Path) -> None:
    """Legacy YAML remains untouched when its protected replacement cannot persist."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = None
        mocks["get_user"].return_value = SimpleNamespace(id="dango-user", metabase_user_id=42)
        mocks["post"].return_value = _session_response("current")
        mocks["store"].save.side_effect = RuntimeError("store unavailable")

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["write_metadata"].assert_not_called()
        mocks["update_password"].assert_not_called()


@pytest.mark.unit
def test_link_preserves_sso_encrypted_password_write(tmp_path: Path) -> None:
    """A successful rotation still stores the Dango user's encrypted SSO password."""
    stack, mocks = _dependencies(tmp_path)
    with stack:
        mocks["store"].load_pending.return_value = None
        mocks["post"].side_effect = [_session_response("current"), _session_response("candidate")]

        _link_metabase_admin(tmp_path, "admin@test.com")

        mocks["encrypt_password"].assert_called_once_with("candidate-password", tmp_path)
        update = mocks["update_user"].call_args.args[2]
        assert update.metabase_user_id == 42
        assert update.metabase_password_enc == "encrypted-candidate"
