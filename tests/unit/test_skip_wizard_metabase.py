"""tests/unit/test_skip_wizard_metabase.py

Tests that `dango init --skip-wizard` creates an admin Metabase accepts (C9), honours
DANGO_ADMIN_PASSWORD (C11), and that setup_metabase_if_needed reports why it skipped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.auth.admin import SKIP_WIZARD_DEFAULT_ADMIN_EMAIL, get_auth_db_path
from dango.auth.database import create_user, list_users
from dango.auth.models import Role, User
from dango.auth.security import verify_password
from dango.cli.init import ProjectInitializer
from dango.platform.common.startup import setup_metabase_if_needed

_STRONG_PASSWORD = "Tr0ub4dor-Horse-Battery-9!"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("DANGO_ADMIN_PASSWORD", raising=False)


def _admins(tmp_path: Path) -> list[User]:
    return [u for u in list_users(get_auth_db_path(tmp_path)) if u.role == Role.ADMIN]


def _init_db(tmp_path: Path) -> Path:
    from dango.migrations.runner import MigrationRunner

    db_path = get_auth_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir).apply_pending()
    return db_path


@pytest.mark.unit
class TestSkipWizardAuth:
    def test_skip_wizard_default_admin_email_has_dotted_domain(self, tmp_path: Path) -> None:
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        (admin,) = _admins(tmp_path)
        assert admin.email == SKIP_WIZARD_DEFAULT_ADMIN_EMAIL
        assert "." in admin.email.split("@")[1]

    def test_skip_wizard_honors_admin_email_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "ops@example.com")
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        (admin,) = _admins(tmp_path)
        assert admin.email == "ops@example.com"

    def test_skip_wizard_uses_admin_password_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", _STRONG_PASSWORD)
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        (admin,) = _admins(tmp_path)
        assert admin.password_hash is not None
        assert verify_password(_STRONG_PASSWORD, admin.password_hash)
        assert admin.must_change_password is False

    def test_skip_wizard_env_password_not_printed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", _STRONG_PASSWORD)
        with patch("dango.cli.init.console") as console:
            ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True)
        assert console.print.called
        for call in console.print.call_args_list:
            assert _STRONG_PASSWORD not in " ".join(str(a) for a in call.args)

    def test_skip_wizard_weak_env_password_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", "weak")
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is False
        assert _admins(tmp_path) == []

    def test_skip_wizard_without_env_password_still_generates(self, tmp_path: Path) -> None:
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        (admin,) = _admins(tmp_path)
        assert admin.must_change_password is True

    def test_skip_wizard_empty_email_env_uses_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "")
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", _STRONG_PASSWORD)
        assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        (admin,) = _admins(tmp_path)
        assert admin.email == SKIP_WIZARD_DEFAULT_ADMIN_EMAIL

    def test_skip_wizard_password_check_ignores_default_email(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A password containing 'admin' is fine when the user never chose the email."""
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", "Admin-Tr0ub4dor-Horse-Battery-9!")
        with patch("dango.cli.init.console"):
            assert ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True) is True
        assert len(_admins(tmp_path)) == 1

    def test_skip_wizard_password_check_uses_chosen_email(self, tmp_path: Path) -> None:
        """When the user sets DANGO_ADMIN_EMAIL, the strength check still receives it."""
        with (
            patch.dict(
                "os.environ",
                {"DANGO_ADMIN_EMAIL": "ops@example.com", "DANGO_ADMIN_PASSWORD": _STRONG_PASSWORD},
            ),
            patch("dango.auth.security.check_password_strength", return_value=[]) as check,
        ):
            ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True)
        check.assert_called_once_with(_STRONG_PASSWORD, email="ops@example.com")

    def test_skip_wizard_password_check_gets_no_email_when_unset(self, tmp_path: Path) -> None:
        with (
            patch.dict("os.environ", {"DANGO_ADMIN_PASSWORD": _STRONG_PASSWORD}),
            patch("dango.auth.security.check_password_strength", return_value=[]) as check,
        ):
            ProjectInitializer(tmp_path)._setup_auth(skip_wizard=True)
        check.assert_called_once_with(_STRONG_PASSWORD, email=None)


def _no_next_restart(logger: MagicMock) -> bool:
    return "next restart" not in str(logger.mock_calls)


@pytest.mark.unit
class TestStartSkipMessage:
    def test_skip_message_shows_reason_and_remedy_not_configured(self) -> None:
        from dango.cli.commands.platform import _print_metabase_skipped

        with patch("dango.cli.commands.platform.console") as console:
            _print_metabase_skipped({"skipped": True, "skip_reason": "REASON-XYZ"})
        out = "\n".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "Metabase was not set up" in out
        assert "REASON-XYZ" in out
        assert "dango auth add-user you@yourcompany.com --role admin --password" in out
        assert "DANGO_ADMIN_EMAIL=you@yourcompany.com dango start" in out
        assert "configured automatically" not in out


@pytest.mark.unit
class TestSetupMetabaseSkipReason:
    def test_setup_metabase_skip_reports_admin_localhost(self, tmp_path: Path) -> None:
        db_path = _init_db(tmp_path)
        create_user(
            db_path,
            User(email="admin@localhost", password_hash="$2b$12$fakehash", role=Role.ADMIN),
        )
        with patch("dango.logging.get_logger") as get_logger:
            result = setup_metabase_if_needed(tmp_path, "P", None)
        assert result["skipped"] is True
        assert result["success"] is True
        assert result["skip_reason"]
        assert _no_next_restart(get_logger.return_value)

    def test_setup_metabase_skip_reports_no_admin(self, tmp_path: Path) -> None:
        with patch("dango.logging.get_logger") as get_logger:
            result = setup_metabase_if_needed(tmp_path, "P", None)
        assert result["skipped"] is True
        assert result["success"] is True
        assert "dango migrate run" in result["skip_reason"]
        assert _no_next_restart(get_logger.return_value)

    def test_setup_metabase_skip_reports_auth_db_read_error(self, tmp_path: Path) -> None:
        _init_db(tmp_path)
        with (
            patch("dango.auth.database.list_users", side_effect=RuntimeError("boom")),
            patch("dango.logging.get_logger") as get_logger,
        ):
            result = setup_metabase_if_needed(tmp_path, "P", None)
        assert result["skipped"] is True
        assert result["skip_reason"]
        assert _no_next_restart(get_logger.return_value)

    def test_setup_metabase_skip_reports_dotless_domain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "ops@corp")
        with patch("dango.logging.get_logger") as get_logger:
            result = setup_metabase_if_needed(tmp_path, "P", None)
        assert result["skipped"] is True
        assert result["success"] is True
        assert "DANGO_ADMIN_EMAIL" in result["skip_reason"]
        assert "corp" in result["skip_reason"]
        assert _no_next_restart(get_logger.return_value)
