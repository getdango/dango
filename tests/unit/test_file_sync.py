"""tests/unit/test_file_sync.py

Unit tests for project file sync (dango/platform/cloud/file_sync.py).

Uses mocked SSHManager and subprocess to avoid real network or
filesystem access.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud.file_sync import (
    REMOTE_PROJECT_DIR,
    SyncResult,
    _build_rsync_ssh_arg,
    _compute_local_hashes,
    _detect_dbt_changes,
    _extract_model_name,
    sync_project_files,
)
from dango.platform.cloud.ssh import CommandResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ssh_mock(*, first_deploy: bool = False) -> MagicMock:
    """Return a mock SSHManager with sensible defaults."""
    ssh = MagicMock()
    ssh.key_path = Path("/tmp/test_key")

    def _exec_side_effect(cmd: str, **kwargs: object) -> CommandResult:
        # First deploy detection
        if f"test -f {REMOTE_PROJECT_DIR}/.dango/sources.yml" in cmd:
            if first_deploy:
                return CommandResult(stdout="", stderr="", exit_code=1)
            return CommandResult(stdout="", stderr="", exit_code=0)
        # Remote hash computation
        if cmd.startswith("find ") and "md5sum" in cmd:
            return CommandResult(stdout="", stderr="", exit_code=0)
        # md5sum for packages.yml check
        if "md5sum" in cmd and "packages.yml" in cmd:
            return CommandResult(
                stdout="abc123  /srv/dango/project/dbt/packages.yml", stderr="", exit_code=0
            )
        # mkdir -p (always succeeds)
        if cmd.startswith("mkdir"):
            return CommandResult(stdout="", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh.exec_command.side_effect = _exec_side_effect
    ssh.upload_file.return_value = None
    return ssh


def _make_project(tmp_path: Path, *, with_packages: bool = True) -> Path:
    """Create a minimal local project structure under *tmp_path*."""
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "sources.yml").write_text("sources: []")
    (root / "dbt" / "models" / "staging").mkdir(parents=True)
    (root / "dbt" / "macros").mkdir(parents=True)
    (root / "dbt" / "dbt_project.yml").write_text("name: test")
    (root / "dbt" / "models" / "staging" / "stg_orders.sql").write_text("SELECT 1")
    (root / "dbt" / "models" / "stg_users.sql").write_text("SELECT 2")
    (root / "dbt" / "macros" / "my_macro.sql").write_text("{% macro foo() %}1{% endmacro %}")
    if with_packages:
        (root / "dbt" / "packages.yml").write_text("packages: []")
    return root


# ---------------------------------------------------------------------------
# SyncResult tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncResult:
    """Test SyncResult dataclass defaults."""

    def test_default_values(self):
        result = SyncResult()
        assert result.synced_files == []
        assert result.changed_models == []
        assert result.added_models == []
        assert result.removed_models == []
        assert result.packages_changed is False
        assert result.is_first_deploy is False
        assert result.dry_run is False


# ---------------------------------------------------------------------------
# Change detection tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChangeDetection:
    """Test dbt change detection helpers."""

    def test_extract_model_name_simple(self):
        assert _extract_model_name("stg_orders.sql") == "stg_orders"

    def test_extract_model_name_nested(self):
        assert _extract_model_name("staging/stg_orders.sql") == "stg_orders"

    def test_detect_added_models(self):
        before: dict[str, str] = {}
        local = {"stg_orders.sql": "abc123"}
        added, changed, removed = _detect_dbt_changes(before, local)
        assert added == ["stg_orders"]
        assert changed == []
        assert removed == []

    def test_detect_changed_models(self):
        before = {"stg_orders.sql": "abc123"}
        local = {"stg_orders.sql": "def456"}
        added, changed, removed = _detect_dbt_changes(before, local)
        assert added == []
        assert changed == ["stg_orders"]
        assert removed == []

    def test_detect_removed_models(self):
        before = {"stg_orders.sql": "abc123"}
        local: dict[str, str] = {}
        added, changed, removed = _detect_dbt_changes(before, local)
        assert added == []
        assert changed == []
        assert removed == ["stg_orders"]

    def test_detect_mixed_changes(self):
        before = {
            "stg_orders.sql": "aaa",
            "stg_deleted.sql": "bbb",
            "stg_unchanged.sql": "ccc",
        }
        local = {
            "stg_orders.sql": "xxx",
            "stg_new.sql": "ddd",
            "stg_unchanged.sql": "ccc",
        }
        added, changed, removed = _detect_dbt_changes(before, local)
        assert added == ["stg_new"]
        assert changed == ["stg_orders"]
        assert removed == ["stg_deleted"]

    def test_no_changes(self):
        hashes = {"stg_orders.sql": "aaa", "stg_users.sql": "bbb"}
        added, changed, removed = _detect_dbt_changes(hashes, dict(hashes))
        assert added == []
        assert changed == []
        assert removed == []


# ---------------------------------------------------------------------------
# Local hash computation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeLocalHashes:
    """Test _compute_local_hashes."""

    def test_computes_hashes(self, tmp_path):
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "stg_orders.sql").write_text("SELECT 1")
        hashes = _compute_local_hashes(models_dir, "*.sql")
        assert "stg_orders.sql" in hashes
        assert len(hashes["stg_orders.sql"]) == 32  # MD5 hex digest

    def test_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        assert _compute_local_hashes(empty_dir, "*.sql") == {}

    def test_nonexistent_directory(self, tmp_path):
        assert _compute_local_hashes(tmp_path / "nope", "*.sql") == {}

    def test_nested_files(self, tmp_path):
        models_dir = tmp_path / "models"
        (models_dir / "staging").mkdir(parents=True)
        (models_dir / "staging" / "stg_orders.sql").write_text("SELECT 1")
        hashes = _compute_local_hashes(models_dir, "*.sql")
        assert "staging/stg_orders.sql" in hashes


# ---------------------------------------------------------------------------
# rsync SSH argument
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildRsyncSshArg:
    """Test _build_rsync_ssh_arg construction."""

    def test_contains_key_path(self):
        arg = _build_rsync_ssh_arg(Path("/home/user/.dango/ssh/key"))
        assert "-i /home/user/.dango/ssh/key" in arg

    def test_disables_host_key_checking(self):
        arg = _build_rsync_ssh_arg(Path("/tmp/key"))
        assert "StrictHostKeyChecking=no" in arg
        assert "UserKnownHostsFile=/dev/null" in arg


# ---------------------------------------------------------------------------
# sync_project_files integration (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncProjectFiles:
    """Test sync_project_files with mocked SSH and subprocess."""

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_first_deploy_all_models_added(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path)

        result = sync_project_files(ssh, project, remote_host="10.0.0.1")

        assert result.is_first_deploy is True
        assert "stg_orders" in result.added_models
        assert "stg_users" in result.added_models
        assert result.changed_models == []
        assert result.removed_models == []
        assert result.packages_changed is True

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_existing_deploy_no_changes(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=False)
        project = _make_project(tmp_path)

        result = sync_project_files(ssh, project, remote_host="10.0.0.1")

        assert result.is_first_deploy is False
        # All models are "added" because remote hashes are empty
        assert len(result.added_models) > 0

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_dry_run_no_upload(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=False)
        project = _make_project(tmp_path)

        result = sync_project_files(ssh, project, remote_host="10.0.0.1", dry_run=True)

        assert result.dry_run is True
        # SFTP upload_file should NOT be called in dry-run
        ssh.upload_file.assert_not_called()
        # rsync should be called with --dry-run flag
        for call in mock_run.call_args_list:
            cmd_list = call[0][0]
            assert "--dry-run" in cmd_list

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_missing_optional_files(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path, with_packages=False)

        result = sync_project_files(ssh, project, remote_host="10.0.0.1")

        # packages.yml not in synced files
        assert "dbt/packages.yml" not in result.synced_files
        # schedules.yml not in synced files (doesn't exist)
        assert ".dango/schedules.yml" not in result.synced_files

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_config_files_uploaded(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path)

        result = sync_project_files(ssh, project, remote_host="10.0.0.1")

        assert ".dango/sources.yml" in result.synced_files
        assert "dbt/dbt_project.yml" in result.synced_files
        assert "dbt/packages.yml" in result.synced_files

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_rsync_command_construction(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path)

        sync_project_files(ssh, project, remote_host="10.0.0.1")

        # rsync should have been called for models and macros dirs
        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            cmd_list = call[0][0]
            assert cmd_list[0] == "rsync"
            assert "-avz" in cmd_list
            assert "--delete" in cmd_list
            # -e flag with SSH key
            e_idx = cmd_list.index("-e")
            ssh_arg = cmd_list[e_idx + 1]
            assert "-i /tmp/test_key" in ssh_arg

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value=None)
    def test_rsync_not_installed(self, mock_which, tmp_path):
        ssh = _make_ssh_mock()
        project = _make_project(tmp_path)

        with pytest.raises(Exception, match="rsync is not installed"):
            sync_project_files(ssh, project, remote_host="10.0.0.1")

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_rsync_failure_raises(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path)

        with pytest.raises(Exception, match="rsync failed"):
            sync_project_files(ssh, project, remote_host="10.0.0.1")

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.file_sync.subprocess.run")
    def test_progress_callback(self, mock_run, mock_which, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ssh = _make_ssh_mock(first_deploy=True)
        project = _make_project(tmp_path)

        steps: list[tuple[str, str]] = []
        sync_project_files(
            ssh, project, remote_host="10.0.0.1", on_progress=lambda s, st: steps.append((s, st))
        )

        step_names = [s for s, _ in steps]
        assert "detect_changes" in step_names
        assert "upload_config" in step_names
        assert "sync_dbt" in step_names
