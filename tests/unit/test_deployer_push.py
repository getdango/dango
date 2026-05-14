"""tests/unit/test_deployer_push.py

Unit tests for push_deploy() orchestration (dango/platform/cloud/deployer.py).

Split from test_deployer.py for file-size compliance. Tests here exercise
the full push_deploy workflow: dry-run, step ordering, error handling,
service restart, and dbt command decisions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.deployer import (
    _DOCKER_FILES,
    DEPLOY_LOCK_PATH,
    _start_all_services,
    push_deploy,
)
from dango.platform.cloud.file_sync import SyncResult
from dango.platform.cloud.ssh import CommandResult
from tests.factories.cloud_factories import make_ssh_mock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sync_result(**kwargs: object) -> SyncResult:
    """Return a SyncResult with defaults."""
    defaults = {
        "synced_files": ["dbt/models/", ".dango/sources.yml"],
        "changed_models": ["stg_orders"],
        "added_models": [],
        "removed_models": [],
        "packages_changed": False,
        "has_macro_changes": False,
        "is_first_deploy": False,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return SyncResult(**defaults)


def _make_backup_result() -> MagicMock:
    """Return a mock BackupResult."""
    result = MagicMock()
    result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"
    result.manifest_path = "/srv/dango/backups/deploy/backup-test.json"
    result.duration_seconds = 5.0
    result.warnings = []
    return result


# ---------------------------------------------------------------------------
# push_deploy tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushDeploy:
    """Test push_deploy orchestration."""

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_dry_run(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(dry_run=True)
        ssh = make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1", dry_run=True)

        assert result.dry_run is True
        assert result.backup_result is None
        mock_backup.assert_not_called()
        mock_sync.assert_called_once()
        assert mock_sync.call_args[1]["dry_run"] is True

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_full_deploy_workflow(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(
            changed_models=["stg_orders"],
            packages_changed=True,
        )
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        commands: list[str] = []

        def _tracking_exec(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _tracking_exec

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.dry_run is False
        assert result.backup_result is not None
        assert result.dbt_deps_run is True
        assert result.dbt_compile_success is True
        assert "stg_orders" in result.models_rebuilt

        # Verify correct step order
        assert any("set -C" in c and DEPLOY_LOCK_PATH in c for c in commands)
        assert any("systemctl stop dango-web" in c for c in commands)
        assert any("chown -R dango:dango" in c for c in commands)
        assert any("systemctl start dango-web" in c for c in commands)
        assert any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in commands)

        # Verify backup called with restart_services=False
        mock_backup.assert_called_once()
        assert mock_backup.call_args[1]["restart_services"] is False

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_dbt_compile_failure_aborts(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "dbt compile" in cmd:
                return CommandResult(stdout="", stderr="Compilation Error", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError, match="dbt compile failed"):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_dbt_run_failure_raises(self, mock_sync, mock_backup, tmp_path):
        """dbt run failure should raise CloudProvisioningError, not just warn."""
        mock_sync.return_value = _make_sync_result(changed_models=["stg_orders"])
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "dbt run" in cmd:
                return CommandResult(stdout="", stderr="Runtime Error", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError, match="dbt run failed"):
            push_deploy(ssh, tmp_path, "10.0.0.1")

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_no_models_changed_skips_dbt_run(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(changed_models=[], added_models=[])
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.models_rebuilt == []
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert not any("dbt run" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_packages_not_changed_skips_dbt_deps(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result(packages_changed=False)
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.dbt_deps_run is False
        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert not any("dbt deps" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_macro_changes_trigger_full_rebuild(self, mock_sync, mock_backup, tmp_path):
        """When macros change, dbt run runs without --select (full rebuild)."""
        mock_sync.return_value = _make_sync_result(
            changed_models=[], added_models=[], has_macro_changes=True
        )
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        dbt_run_calls = [c for c in all_calls if "dbt run" in c]
        assert len(dbt_run_calls) == 1
        assert "--select" not in dbt_run_calls[0]
        assert result.models_rebuilt == ["(full rebuild \u2014 macros changed)"]

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_lock_released_on_error(self, mock_sync, mock_backup, tmp_path):
        mock_backup.side_effect = CloudProvisioningError("backup failed")
        ssh = make_ssh_mock()

        with pytest.raises(CloudProvisioningError, match="backup failed"):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert any(f"rm -f {DEPLOY_LOCK_PATH}" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_services_restarted_on_error(self, mock_sync, mock_backup, tmp_path):
        """Services are restarted in finally block even when deploy fails."""
        mock_backup.return_value = _make_backup_result()
        mock_sync.return_value = _make_sync_result()
        ssh = make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "dbt compile" in cmd:
                return CommandResult(stdout="", stderr="fail", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        all_calls = [args[0][0] for args in ssh.exec_command.call_args_list]
        assert any("systemctl start dango-web" in c for c in all_calls)
        assert any("start metabase" in c for c in all_calls)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_progress_callback(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        steps: list[tuple[str, str]] = []
        push_deploy(ssh, tmp_path, "10.0.0.1", on_progress=lambda s, st: steps.append((s, st)))

        step_names = [s for s, _ in steps]
        assert "acquire_lock" in step_names
        assert "stop_web" in step_names
        assert "create_backup" in step_names
        assert "sync_files" in step_names
        assert "fix_ownership" in step_names
        assert "dbt_compile" in step_names
        assert "start_services" in step_names

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_source_validation_warnings(self, mock_sync, mock_backup, tmp_path):
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "sources.yml" in cmd and "cat" in cmd:
                return CommandResult(
                    stdout="sources:\n  - name: missing_source\n",
                    stderr="",
                    exit_code=0,
                )
            if "secrets.toml" in cmd and "cat" in cmd:
                return CommandResult(stdout="# empty", stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        result = push_deploy(ssh, tmp_path, "10.0.0.1")
        assert any("missing_source" in w for w in result.warnings)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_git_info_passthrough(self, mock_sync, mock_backup, tmp_path):
        """git_info should be stored in DeployResult."""
        mock_sync.return_value = _make_sync_result(dry_run=True)
        ssh = make_ssh_mock()

        git_info = MagicMock()
        git_info.commit_sha = "abc12345" * 5
        git_info.branch = "main"

        result = push_deploy(ssh, tmp_path, "10.0.0.1", dry_run=True, git_info=git_info)
        assert result.git_info is git_info
        assert result.git_info.commit_sha == "abc12345" * 5

    @patch("dango.platform.cloud.deployer._write_deploy_journal")
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_journal_written_on_success(self, mock_sync, mock_backup, mock_journal, tmp_path):
        """Journal should be written on successful deploy with git_info."""
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        git_info = MagicMock()
        git_info.commit_sha = "a" * 40
        git_info.branch = "main"
        git_info.is_clean = True
        git_info.remote_url = "git@github.com:test/test.git"

        push_deploy(ssh, tmp_path, "10.0.0.1", git_info=git_info)

        mock_journal.assert_called_once()
        call_kwargs = mock_journal.call_args[1]
        assert call_kwargs["deploy_succeeded"] is True
        assert call_kwargs["deploy_error"] is None

    @patch("dango.platform.cloud.deployer._write_deploy_journal")
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_journal_not_written_on_dry_run(self, mock_sync, mock_backup, mock_journal, tmp_path):
        """Journal should NOT be written on dry-run."""
        mock_sync.return_value = _make_sync_result(dry_run=True)
        ssh = make_ssh_mock()

        git_info = MagicMock()
        git_info.commit_sha = "a" * 40
        git_info.branch = "main"

        push_deploy(ssh, tmp_path, "10.0.0.1", dry_run=True, git_info=git_info)

        mock_journal.assert_not_called()

    @patch("dango.platform.cloud.deployer._write_deploy_journal")
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_journal_written_without_git_info(self, mock_sync, mock_backup, mock_journal, tmp_path):
        """Journal should be written even when git_info is None."""
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        push_deploy(ssh, tmp_path, "10.0.0.1")

        mock_journal.assert_called_once()
        call_args = mock_journal.call_args
        assert call_args[0][2] is None  # git_info positional arg

    @patch(
        "dango.platform.cloud.deployer._write_deploy_journal", side_effect=Exception("journal boom")
    )
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_journal_failure_does_not_break_deploy(
        self, mock_sync, mock_backup, mock_journal, tmp_path
    ):
        """Deploy should succeed even if journal write raises."""
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        result = push_deploy(ssh, tmp_path, "10.0.0.1")

        assert result.sync_result is not None
        assert result.dry_run is False

    @patch("dango.platform.cloud.deployer._write_deploy_journal")
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_journal_written_on_failed_deploy(self, mock_sync, mock_backup, mock_journal, tmp_path):
        """Journal should be written with deploy_succeeded=False on failure."""
        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "dbt compile" in cmd:
                return CommandResult(stdout="", stderr="fail", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _side_effect

        with pytest.raises(CloudProvisioningError):
            push_deploy(ssh, tmp_path, "10.0.0.1")

        mock_journal.assert_called_once()
        call_kwargs = mock_journal.call_args[1]
        assert call_kwargs["deploy_succeeded"] is False
        assert call_kwargs["deploy_error"] is not None
        assert "dbt compile failed" in call_kwargs["deploy_error"]


# ---------------------------------------------------------------------------
# Docker rebuild detection tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDockerRebuild:
    """Test Docker rebuild detection in _start_all_services and push_deploy."""

    def test_docker_files_constant(self):
        """_DOCKER_FILES contains the expected Docker filenames."""
        assert "docker-compose.yml" in _DOCKER_FILES
        assert "Dockerfile.metabase" in _DOCKER_FILES
        assert "entrypoint.sh" in _DOCKER_FILES

    def test_start_all_services_rebuild_true(self):
        """With rebuild_docker=True, uses 'up --build -d'."""
        ssh = make_ssh_mock()
        commands: list[str] = []

        def _track(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _track

        _start_all_services(ssh, rebuild_docker=True)

        assert any("up --build -d" in c for c in commands)
        # Should NOT use 'start metabase' pattern
        assert not any("start metabase 2>/dev/null" in c for c in commands)
        # Should still start dango-web
        assert any("systemctl start dango-web" in c for c in commands)

    def test_start_all_services_rebuild_false(self):
        """With rebuild_docker=False, uses 'start metabase' (no rebuild)."""
        ssh = make_ssh_mock()
        commands: list[str] = []

        def _track(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _track

        _start_all_services(ssh, rebuild_docker=False)

        assert any("start metabase" in c for c in commands)
        assert not any("up --build" in c for c in commands)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_push_deploy_detects_docker_changes(self, mock_sync, mock_backup, tmp_path):
        """push_deploy triggers Docker rebuild when Docker files are in synced_files."""
        mock_sync.return_value = _make_sync_result(
            synced_files=["docker-compose.yml", "dbt/models/", ".dango/sources.yml"],
        )
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        commands: list[str] = []

        def _tracking_exec(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _tracking_exec

        push_deploy(ssh, tmp_path, "10.0.0.1")

        assert any("up --build -d" in c for c in commands)

    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_push_deploy_no_docker_changes(self, mock_sync, mock_backup, tmp_path):
        """push_deploy does NOT rebuild Docker when no Docker files changed."""
        mock_sync.return_value = _make_sync_result(
            synced_files=["dbt/models/", ".dango/sources.yml"],
        )
        mock_backup.return_value = _make_backup_result()
        ssh = make_ssh_mock()

        commands: list[str] = []

        def _tracking_exec(cmd: str, **kwargs: object) -> CommandResult:
            commands.append(cmd)
            return CommandResult(stdout="", stderr="", exit_code=0)

        ssh.exec_command.side_effect = _tracking_exec

        push_deploy(ssh, tmp_path, "10.0.0.1")

        assert not any("up --build" in c for c in commands)
        assert any("start metabase" in c for c in commands)
