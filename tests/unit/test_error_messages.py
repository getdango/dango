"""tests/unit/test_error_messages.py

Unit tests for P7-013 structured error messages.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import (
    ConfigNotFoundError,
    DuckDBHealthError,
    JobCancelledError,
    OAuthTokenExpiredError,
    OAuthTokenRevokedError,
    format_structured_error,
)


def _assert_structured(msg: str) -> None:
    """Assert a message has the 3-section structured format."""
    assert "Possible causes:" in msg
    assert "Suggested fix:" in msg


# ---------------------------------------------------------------------------
# format_structured_error helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatStructuredError:
    """Tests for the format_structured_error helper."""

    def test_produces_three_sections(self) -> None:
        result = format_structured_error(
            what_failed="Something broke",
            causes=["Cause A", "Cause B"],
            suggested_fix="Fix it",
        )
        assert result.startswith("Something broke")
        assert "Possible causes:" in result
        assert "  - Cause A" in result
        assert "  - Cause B" in result
        assert "Suggested fix: Fix it" in result

    def test_single_cause(self) -> None:
        result = format_structured_error(
            what_failed="Fail", causes=["Only cause"], suggested_fix="Do this"
        )
        assert "  - Only cause" in result
        _assert_structured(result)


# ---------------------------------------------------------------------------
# DuckDB health errors (db_health.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDuckDBErrorMessages:
    """Tests for DuckDB health check structured errors."""

    def test_lock_error_classified(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test.duckdb"
        db_file.write_bytes(b"fake")
        with patch("dango.utils.db_health.duckdb") as mock_duckdb:
            mock_duckdb.connect.side_effect = Exception(
                "database already open by another process (lock)"
            )
            with pytest.raises(DuckDBHealthError) as exc_info:
                from dango.utils.db_health import check_duckdb_health

                check_duckdb_health(db_file)
            _assert_structured(exc_info.value.user_message)
            assert "write lock" in exc_info.value.user_message

    def test_permission_error_classified(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test.duckdb"
        db_file.write_bytes(b"fake")
        with patch("dango.utils.db_health.duckdb") as mock_duckdb:
            mock_duckdb.connect.side_effect = Exception("Permission denied")
            with pytest.raises(DuckDBHealthError) as exc_info:
                from dango.utils.db_health import check_duckdb_health

                check_duckdb_health(db_file)
            _assert_structured(exc_info.value.user_message)
            assert "permission" in exc_info.value.user_message.lower()

    def test_not_found_error_classified(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test.duckdb"
        db_file.write_bytes(b"fake")
        with patch("dango.utils.db_health.duckdb") as mock_duckdb:
            mock_duckdb.connect.side_effect = Exception("no such file or directory")
            with pytest.raises(DuckDBHealthError) as exc_info:
                from dango.utils.db_health import check_duckdb_health

                check_duckdb_health(db_file)
            _assert_structured(exc_info.value.user_message)
            assert "does not exist" in exc_info.value.user_message

    def test_generic_error_classified(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test.duckdb"
        db_file.write_bytes(b"fake")
        with patch("dango.utils.db_health.duckdb") as mock_duckdb:
            mock_duckdb.connect.side_effect = Exception("something unexpected")
            with pytest.raises(DuckDBHealthError) as exc_info:
                from dango.utils.db_health import check_duckdb_health

                check_duckdb_health(db_file)
            _assert_structured(exc_info.value.user_message)


# ---------------------------------------------------------------------------
# dbt errors (transformation/__init__.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDbtErrorMessages:
    """Tests for dbt compilation structured errors."""

    @patch("dango.transformation.subprocess.run")
    @patch("dango.transformation._get_dbt_executable", return_value="dbt")
    def test_compilation_error_structured(self, _mock_exec: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Compilation Error in model foo\n",
            stderr="",
        )
        from dango.transformation import run_dbt_models

        success, output = run_dbt_models(Path("/tmp/fake"))
        assert not success
        _assert_structured(output)
        assert "dbt run failed" in output

    @patch("dango.transformation.subprocess.run")
    @patch("dango.transformation._get_dbt_executable", return_value="dbt")
    def test_timeout_structured(self, _mock_exec: MagicMock, mock_run: MagicMock) -> None:
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="dbt run", timeout=300)
        from dango.transformation import run_dbt_models

        success, output = run_dbt_models(Path("/tmp/fake"))
        assert not success
        _assert_structured(output)
        assert "timed out" in output


# ---------------------------------------------------------------------------
# Docker errors (platform/docker.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDockerErrorMessages:
    """Tests for DockerManager.start_services() structured errors."""

    def test_docker_not_available(self) -> None:
        from dango.platform.docker import DockerManager

        mgr = DockerManager(Path("/tmp/fake"))
        mgr.compose_file = MagicMock(exists=MagicMock(return_value=True))
        with patch.object(mgr, "is_docker_available", return_value=False):
            with patch("dango.platform.docker.console") as mock_console:
                result = mgr.start_services()
                assert result is False
                printed = mock_console.print.call_args_list[0][0][0]
                _assert_structured(printed)
                assert "Docker is not available" in printed

    def test_compose_not_available(self) -> None:
        from dango.platform.docker import DockerManager

        mgr = DockerManager(Path("/tmp/fake"))
        mgr.compose_file = MagicMock(exists=MagicMock(return_value=True))
        with (
            patch.object(mgr, "is_docker_available", return_value=True),
            patch.object(mgr, "is_compose_available", return_value=False),
            patch("dango.platform.docker.console") as mock_console,
        ):
            result = mgr.start_services()
            assert result is False
            printed = mock_console.print.call_args_list[0][0][0]
            _assert_structured(printed)
            assert "Docker Compose" in printed


# ---------------------------------------------------------------------------
# OAuth errors (oauth/validation.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthErrorMessages:
    """Tests for OAuth token validation structured errors."""

    @patch("dango.oauth.validation.validate_token")
    @patch("dango.oauth.validation.OAuthStorage")
    def test_expired_token_structured(
        self, mock_storage_cls: MagicMock, mock_validate: MagicMock
    ) -> None:
        from dango.oauth.validation import TokenValidationResult, validate_before_sync

        mock_storage = mock_storage_cls.return_value
        mock_cred = MagicMock()
        mock_storage.get.return_value = mock_cred
        mock_validate.return_value = TokenValidationResult(
            source_type="google_sheets",
            provider="google",
            valid=False,
            message="Token expired",
            error_code="expired",
        )
        with (
            patch("dango.oauth.validation.OAUTH_PROVIDER_MAP", {"google_sheets": "google"}),
            pytest.raises(OAuthTokenExpiredError) as exc_info,
        ):
            validate_before_sync("google_sheets", Path("/tmp/fake"))
        _assert_structured(exc_info.value.user_message)

    @patch("dango.oauth.validation.validate_token")
    @patch("dango.oauth.validation.OAuthStorage")
    def test_revoked_token_structured(
        self, mock_storage_cls: MagicMock, mock_validate: MagicMock
    ) -> None:
        from dango.oauth.validation import TokenValidationResult, validate_before_sync

        mock_storage = mock_storage_cls.return_value
        mock_cred = MagicMock()
        mock_storage.get.return_value = mock_cred
        mock_validate.return_value = TokenValidationResult(
            source_type="google_sheets",
            provider="google",
            valid=False,
            message="Token revoked",
            error_code="revoked",
        )
        with (
            patch("dango.oauth.validation.OAUTH_PROVIDER_MAP", {"google_sheets": "google"}),
            pytest.raises(OAuthTokenRevokedError) as exc_info,
        ):
            validate_before_sync("google_sheets", Path("/tmp/fake"))
        _assert_structured(exc_info.value.user_message)


# ---------------------------------------------------------------------------
# Scheduler errors (platform/scheduling/resilience.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchedulerErrorMessages:
    """Tests for scheduler cancelled/timeout structured errors."""

    def test_cancelled_before_execution(self) -> None:
        import threading

        from dango.platform.scheduling.resilience import _execute_with_timeout

        flag = threading.Event()
        flag.set()
        with pytest.raises(JobCancelledError) as exc_info:
            _execute_with_timeout(lambda: None, (), {}, 60, flag)
        _assert_structured(exc_info.value.user_message)

    def test_cancelled_before_attempt(self) -> None:
        import threading

        from dango.platform.scheduling.resilience import run_with_resilience

        mock_svc = MagicMock()
        flag = threading.Event()
        flag.set()
        mock_svc._register_cancel_flag.return_value = flag
        with pytest.raises(JobCancelledError) as exc_info:
            run_with_resilience(lambda: None, scheduler_service=mock_svc, job_id="test-job")
        _assert_structured(exc_info.value.user_message)


# ---------------------------------------------------------------------------
# Backup errors (cli/commands/remote_backup.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupErrorMessages:
    """Tests for backup failure structured console messages."""

    def test_backup_module_uses_structured_errors(self) -> None:
        """Verify remote_backup module imports format_structured_error."""
        import dango.cli.commands.remote_backup as mod

        # Module-level import of format_structured_error confirms integration
        assert hasattr(mod, "format_structured_error")

    def test_restore_failure_message_format(self) -> None:
        """Verify the restore failure message is structured."""
        msg = format_structured_error(
            what_failed="Restore failed from backup 'test.tar.gz'",
            causes=[
                "Backup archive is corrupt or incomplete",
                "Insufficient disk space on server",
                "Services failed to restart after restore",
            ],
            suggested_fix="Check server disk with 'dango remote status' and try a different backup",
        )
        _assert_structured(msg)
        assert "Restore failed" in msg


# ---------------------------------------------------------------------------
# Config errors (config/loader.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigErrorMessages:
    """Tests for config validation structured errors."""

    def test_config_not_found_structured(self) -> None:
        from dango.config.loader import ConfigLoader

        loader = ConfigLoader(Path("/tmp/nonexistent"))
        with pytest.raises(ConfigNotFoundError) as exc_info:
            loader.load_yaml(Path("/tmp/nonexistent/project.yml"))
        _assert_structured(exc_info.value.user_message)

    def test_invalid_yaml_structured(self, tmp_path: Path) -> None:
        from dango.config.loader import ConfigLoader

        bad_yaml = tmp_path / "bad.yml"
        bad_yaml.write_text(":\n  invalid: [yaml\n")
        loader = ConfigLoader(tmp_path)
        from dango.exceptions import ConfigError

        with pytest.raises(ConfigError) as exc_info:
            loader.load_yaml(bad_yaml)
        _assert_structured(exc_info.value.user_message)
