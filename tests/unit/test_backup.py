"""tests/unit/test_backup.py

Unit tests for dango/platform/cloud/backup.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from tests.factories.cloud_factories import make_ssh_mock_configurable


@pytest.mark.unit
class TestBackupDataClasses:
    def test_backup_manifest_construction(self):
        """BackupManifest can be created with required fields."""
        from dango.platform.cloud.backup import BackupManifest

        m = BackupManifest(
            timestamp="20260224-143000",
            backup_type="pre-deploy",
            dango_version="0.1.1",
        )
        assert m.timestamp == "20260224-143000"
        assert m.backup_type == "pre-deploy"
        assert m.dango_version == "0.1.1"
        assert m.files == []
        assert m.total_size_bytes == 0

    def test_backup_manifest_frozen(self):
        """BackupManifest is immutable."""
        from dango.platform.cloud.backup import BackupManifest

        m = BackupManifest(
            timestamp="20260224-143000",
            backup_type="pre-deploy",
            dango_version="0.1.1",
        )
        with pytest.raises(AttributeError):
            m.timestamp = "changed"  # type: ignore[misc]

    def test_backup_result_construction(self):
        """BackupResult stores archive path and manifest."""
        from dango.platform.cloud.backup import BackupManifest, BackupResult

        m = BackupManifest(
            timestamp="20260224-143000",
            backup_type="pre-deploy",
            dango_version="0.1.1",
        )
        r = BackupResult(
            archive_path="/srv/dango/backups/deploy/backup-20260224-143000.tar.gz",
            manifest_path="/srv/dango/backups/deploy/backup-20260224-143000.json",
            manifest=m,
            duration_seconds=12.5,
        )
        assert r.archive_path.endswith(".tar.gz")
        assert r.warnings == []

    def test_restore_result_construction(self):
        """RestoreResult stores health check status."""
        from dango.platform.cloud.backup import RestoreResult

        r = RestoreResult(
            restored_from="/srv/dango/backups/deploy/backup-20260224-143000.tar.gz",
            services_restarted=True,
            health_check_passed=True,
            duration_seconds=25.0,
        )
        assert r.health_check_passed is True
        assert r.warnings == []


@pytest.mark.unit
class TestBackupHelpers:
    def test_check_disk_space_sufficient(self):
        """No error when disk space is sufficient."""
        from dango.platform.cloud.backup import _check_disk_space

        ssh = make_ssh_mock_configurable(
            exec_results={"df -m": ("/dev/vda1 25000 5000 20000 20% /srv", "", 0)}
        )
        _check_disk_space(ssh)  # Should not raise

    def test_check_disk_space_insufficient(self):
        """Raises CloudProvisioningError when disk space is low."""
        from dango.platform.cloud.backup import _check_disk_space

        ssh = make_ssh_mock_configurable(
            exec_results={"df -m": ("/dev/vda1 25000 24700 300 99% /srv", "", 0)}
        )
        with pytest.raises(CloudProvisioningError, match="Insufficient disk space"):
            _check_disk_space(ssh)

    def test_stop_services_calls_systemctl_and_docker(self):
        """stop_services stops dango-web and metabase."""
        from dango.platform.cloud.backup import stop_services

        ssh = make_ssh_mock_configurable()
        stop_services(ssh)

        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("systemctl stop dango-web" in cmd for cmd in cmds)
        assert any("docker compose" in cmd and "stop metabase" in cmd for cmd in cmds)

    def test_start_services_starts_metabase_then_web(self):
        """start_services starts metabase first, then dango-web."""
        from dango.platform.cloud.backup import start_services

        ssh = make_ssh_mock_configurable()
        start_services(ssh)

        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        metabase_idx = next(i for i, c in enumerate(cmds) if "start metabase" in c)
        web_idx = next(i for i, c in enumerate(cmds) if "systemctl start dango-web" in c)
        assert metabase_idx < web_idx

    def test_checkpoint_duckdb_success(self):
        """_checkpoint_duckdb runs CHECKPOINT when file exists."""
        from dango.platform.cloud.backup import _checkpoint_duckdb

        ssh = make_ssh_mock_configurable(exec_results={"test -f": ("", "", 0)})
        result = _checkpoint_duckdb(ssh)

        assert result is True
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("CHECKPOINT" in cmd for cmd in cmds)

    def test_checkpoint_duckdb_missing(self):
        """_checkpoint_duckdb returns False when file doesn't exist."""
        from dango.platform.cloud.backup import _checkpoint_duckdb

        ssh = make_ssh_mock_configurable(exec_results={"test -f": ("", "", 1)})
        result = _checkpoint_duckdb(ssh)
        assert result is False

    def test_checkpoint_auth_db_success(self):
        """_checkpoint_auth_db runs WAL checkpoint when file exists."""
        from dango.platform.cloud.backup import _checkpoint_auth_db

        ssh = make_ssh_mock_configurable(exec_results={"test -f": ("", "", 0)})
        result = _checkpoint_auth_db(ssh)

        assert result is True
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("wal_checkpoint" in cmd for cmd in cmds)

    def test_checkpoint_auth_db_missing(self):
        """_checkpoint_auth_db returns False when file doesn't exist."""
        from dango.platform.cloud.backup import _checkpoint_auth_db

        ssh = make_ssh_mock_configurable(exec_results={"test -f": ("", "", 1)})
        result = _checkpoint_auth_db(ssh)
        assert result is False

    def test_get_metabase_volume_path_found(self):
        """Returns volume path when docker volume inspect succeeds."""
        from dango.platform.cloud.backup import _get_metabase_volume_path

        ssh = make_ssh_mock_configurable(
            exec_results={"docker volume inspect": ("/var/lib/docker/volumes/vol/_data", "", 0)}
        )
        path = _get_metabase_volume_path(ssh)
        assert path == "/var/lib/docker/volumes/vol/_data"

    def test_get_metabase_volume_path_not_found(self):
        """Returns None when docker volume doesn't exist."""
        from dango.platform.cloud.backup import _get_metabase_volume_path

        ssh = make_ssh_mock_configurable(
            exec_results={"docker volume inspect": ("", "not found", 1)}
        )
        path = _get_metabase_volume_path(ssh)
        assert path is None

    def test_verify_health_success(self):
        """verify_health returns True when endpoint responds."""
        from dango.platform.cloud.backup import verify_health

        ssh = make_ssh_mock_configurable(exec_results={"curl": ('{"status":"ok"}', "", 0)})
        result = verify_health(ssh, timeout=10)
        assert result is True

    @patch("dango.platform.cloud.backup.time.sleep")
    @patch("dango.platform.cloud.backup.time.monotonic")
    def test_verify_health_timeout(self, mock_monotonic, mock_sleep):
        """verify_health returns False after timeout."""
        from dango.platform.cloud.backup import verify_health

        # Simulate timeout: first call returns start time, second call exceeds timeout
        mock_monotonic.side_effect = [0.0, 100.0]

        ssh = make_ssh_mock_configurable(exec_results={"curl": ("", "connection refused", 1)})
        result = verify_health(ssh, timeout=90)
        assert result is False


@pytest.mark.unit
class TestCreateBackup:
    @patch("dango.platform.cloud.backup.time.strftime", return_value="20260224-143000")
    def test_full_success_flow(self, mock_strftime):
        """create_backup completes all steps and returns BackupResult."""
        from dango.platform.cloud.backup import create_backup

        ssh = make_ssh_mock_configurable(
            exec_results={
                "df -m": ("/dev/vda1 25000 5000 20000 20% /srv", "", 0),
                "test -f": ("", "", 0),
                "docker volume inspect": ("/var/lib/docker/vol/_data", "", 0),
                "import dango": ("0.1.1", "", 0),
                "find /tmp": ("", "", 0),
                "ls -1t": ("", "", 0),
            }
        )

        result = create_backup(ssh, backup_type="pre-deploy")

        assert result.archive_path.endswith(".tar.gz")
        assert result.manifest.backup_type == "pre-deploy"
        assert result.manifest.timestamp == "20260224-143000"
        assert result.duration_seconds >= 0

    @patch("dango.platform.cloud.backup.time.strftime", return_value="20260224-143000")
    def test_services_restarted_on_failure(self, mock_strftime):
        """Services are restarted even when archive creation fails."""
        from dango.platform.cloud.backup import create_backup

        ssh = make_ssh_mock_configurable(
            exec_results={
                "df -m": ("/dev/vda1 25000 5000 20000 20% /srv", "", 0),
                "test -f": ("", "", 0),
                "docker volume inspect": ("/var/lib/docker/vol/_data", "", 0),
                "mkdir -p /tmp/backup": ("", "disk full", 1),
            }
        )

        with pytest.raises(CloudProvisioningError):
            create_backup(ssh)

        # Verify services were restarted (in finally block)
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("systemctl start dango-web" in cmd for cmd in cmds)

    @patch("dango.platform.cloud.backup.time.strftime", return_value="20260224-143000")
    def test_missing_duckdb_adds_warning(self, mock_strftime):
        """Missing DuckDB file produces a warning, not an error."""
        from dango.platform.cloud.backup import create_backup

        ssh = make_ssh_mock_configurable(
            exec_results={
                "df -m": ("/dev/vda1 25000 5000 20000 20% /srv", "", 0),
                # test -f fails for DuckDB
                "test -f /srv/dango/project/data/warehouse.duckdb": ("", "", 1),
                "test -f /srv/dango/project/.dango/auth.db": ("", "", 1),
                "docker volume inspect": ("", "not found", 1),
                "import dango": ("0.1.1", "", 0),
                "find /tmp": ("", "", 0),
                "ls -1t": ("", "", 0),
            }
        )

        result = create_backup(ssh)

        assert any("DuckDB" in w for w in result.warnings)
        assert any("Auth database" in w for w in result.warnings)

    @patch("dango.platform.cloud.backup.time.strftime", return_value="20260224-143000")
    def test_progress_callback_called(self, mock_strftime):
        """on_progress callback is called for each step."""
        from dango.platform.cloud.backup import create_backup

        ssh = make_ssh_mock_configurable(
            exec_results={
                "df -m": ("/dev/vda1 25000 5000 20000 20% /srv", "", 0),
                "test -f": ("", "", 0),
                "docker volume inspect": ("/var/lib/docker/vol/_data", "", 0),
                "import dango": ("0.1.1", "", 0),
                "find /tmp": ("", "", 0),
                "ls -1t": ("", "", 0),
            }
        )
        progress: list[tuple[str, str]] = []

        create_backup(ssh, on_progress=lambda s, st: progress.append((s, st)))

        step_names = [s for s, _ in progress]
        assert "check_disk_space" in step_names
        assert "stop_services" in step_names
        assert "create_archive" in step_names
        assert "start_services" in step_names


@pytest.mark.unit
class TestListLocalBackups:
    def test_returns_sorted_backups(self):
        """Backups are returned newest-first from ls -1t output."""
        from dango.platform.cloud.backup import list_local_backups

        ssh = make_ssh_mock_configurable(
            exec_results={
                "ls -1t": (
                    "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz\n"
                    "/srv/dango/backups/deploy/backup-20260223-020000.tar.gz\n",
                    "",
                    0,
                ),
                "stat": ("12345", "", 0),
            }
        )
        backups = list_local_backups(ssh)

        assert len(backups) == 2
        assert backups[0]["name"] == "backup-20260224-143000.tar.gz"
        assert backups[0]["date"] == "20260224-143000"
        assert backups[1]["name"] == "backup-20260223-020000.tar.gz"

    def test_empty_dir_returns_empty(self):
        """Empty backup directory returns empty list."""
        from dango.platform.cloud.backup import list_local_backups

        ssh = make_ssh_mock_configurable(exec_results={"ls -1t": ("", "", 0)})
        backups = list_local_backups(ssh)
        assert backups == []


@pytest.mark.unit
class TestRollback:
    def test_uses_most_recent_backup(self):
        """rollback() picks the most recent backup when no path specified."""
        from dango.platform.cloud.backup import rollback

        ssh = make_ssh_mock_configurable(
            exec_results={
                "ls -1t": (
                    "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz\n",
                    "",
                    0,
                ),
                "stat": ("12345", "", 0),
                "cat /srv/dango/backups/deploy/backup-20260224-143000.json": (
                    '{"timestamp":"20260224-143000"}',
                    "",
                    0,
                ),
                "docker volume inspect": ("/var/lib/docker/vol/_data", "", 0),
                "curl": ('{"status":"ok"}', "", 0),
            }
        )

        result = rollback(ssh)

        assert result.restored_from.endswith("backup-20260224-143000.tar.gz")
        assert result.services_restarted is True

    def test_specific_backup_path(self):
        """rollback() uses the specified backup path."""
        from dango.platform.cloud.backup import rollback

        archive = "/srv/dango/backups/deploy/backup-20260223-020000.tar.gz"
        ssh = make_ssh_mock_configurable(
            exec_results={
                f"test -f {archive}": ("", "", 0),
                f"cat {archive.replace('.tar.gz', '.json')}": ("", "", 1),
                "docker volume inspect": ("/var/lib/docker/vol/_data", "", 0),
                "curl": ('{"status":"ok"}', "", 0),
            }
        )

        result = rollback(ssh, backup_path=archive)

        assert result.restored_from == archive

    def test_no_backups_raises(self):
        """rollback() raises when no backups exist."""
        from dango.platform.cloud.backup import rollback

        ssh = make_ssh_mock_configurable(exec_results={"ls -1t": ("", "", 0)})

        with pytest.raises(CloudProvisioningError, match="No backups found"):
            rollback(ssh)

    def test_backup_not_found_raises(self):
        """rollback() raises when specific path doesn't exist."""
        from dango.platform.cloud.backup import rollback

        ssh = make_ssh_mock_configurable(exec_results={"test -f": ("", "", 1)})

        with pytest.raises(CloudProvisioningError, match="not found"):
            rollback(ssh, backup_path="/srv/dango/backups/deploy/nonexistent.tar.gz")

    def test_services_restarted_on_failure(self):
        """Services are restarted even when extraction fails."""
        from dango.platform.cloud.backup import rollback

        ssh = make_ssh_mock_configurable(
            exec_results={
                "ls -1t": (
                    "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz\n",
                    "",
                    0,
                ),
                "stat": ("12345", "", 0),
                "cat /srv/dango/backups/deploy/backup-20260224-143000.json": ("", "", 1),
                "tar -xzf": ("", "corrupted archive", 1),
            }
        )

        with pytest.raises(CloudProvisioningError):
            rollback(ssh)

        # Verify services were restarted
        cmds = [c[0][0] for c in ssh.exec_command.call_args_list]
        assert any("systemctl start dango-web" in cmd for cmd in cmds)


@pytest.mark.unit
class TestRotateLocalBackups:
    def test_keeps_max_deletes_old(self):
        """Rotates out archives beyond the keep limit."""
        from dango.platform.cloud.backup import rotate_local_backups

        files = "\n".join(
            f"/srv/dango/backups/deploy/backup-2026022{i}-020000.tar.gz" for i in range(7)
        )
        ssh = make_ssh_mock_configurable(exec_results={"ls -1t": (files, "", 0)})

        deleted = rotate_local_backups(ssh, keep=5)

        assert deleted == 2

    def test_fewer_than_limit_no_deletion(self):
        """No deletions when archives count is within limit."""
        from dango.platform.cloud.backup import rotate_local_backups

        ssh = make_ssh_mock_configurable(
            exec_results={
                "ls -1t": (
                    "/srv/dango/backups/deploy/backup-20260224-143000.tar.gz\n"
                    "/srv/dango/backups/deploy/backup-20260223-020000.tar.gz\n",
                    "",
                    0,
                )
            }
        )

        deleted = rotate_local_backups(ssh, keep=5)

        assert deleted == 0

    def test_empty_dir_returns_zero(self):
        """Empty directory returns zero deletions."""
        from dango.platform.cloud.backup import rotate_local_backups

        ssh = make_ssh_mock_configurable(exec_results={"ls -1t": ("", "", 0)})

        deleted = rotate_local_backups(ssh, keep=5)

        assert deleted == 0
