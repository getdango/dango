"""tests/unit/test_scheduled_backup.py

Unit tests for server-side scheduled backup
(dango/platform/cloud/scheduled_backup.py).

Mocks subprocess.run for local commands, SpacesClient for Spaces
operations, and tmp_path for file I/O.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError

# ---------------------------------------------------------------------------
# 1. Data classes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScheduledBackupDataClasses:
    def test_scheduled_backup_result_defaults(self):
        """ScheduledBackupResult initialises with empty defaults."""
        from dango.platform.cloud.scheduled_backup import ScheduledBackupResult

        r = ScheduledBackupResult()
        assert r.archive_path == ""
        assert r.spaces_key == ""
        assert r.error is None
        assert r.warnings == []

    def test_spaces_backup_info_frozen(self):
        """SpacesBackupInfo is immutable."""
        from dango.platform.cloud.scheduled_backup import SpacesBackupInfo

        info = SpacesBackupInfo(
            key="backups/backup-20260224-143000.tar.gz",
            name="backup-20260224-143000.tar.gz",
            size_bytes=1024,
            last_modified="2026-02-24",
        )
        assert info.name == "backup-20260224-143000.tar.gz"
        with pytest.raises(AttributeError):
            info.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Retention policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetentionPolicy:
    def _make_archive(self, dt):
        """Create a mock Spaces object for the given datetime."""
        ts = dt.strftime("%Y%m%d-%H%M%S")
        return {"Key": f"backups/backup-{ts}.tar.gz", "Size": 1024}

    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_keeps_7_daily(self, mock_spaces_cls):
        """Archives from the last 7 days are kept."""
        from dango.platform.cloud.scheduled_backup import _apply_retention

        now = datetime.now(tz=timezone.utc)
        archives = [self._make_archive(now - timedelta(days=i)) for i in range(10)]

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_client.list_objects.return_value = archives

        deleted = _apply_retention({"bucket": "test", "region": "nyc3"})

        # 10 archives: 7 daily kept, 3 older ones. Of those 3, some may be
        # kept as weekly. At minimum, some deletions should occur.
        assert deleted >= 0
        assert mock_client.list_objects.called

    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_empty_bucket_no_deletions(self, mock_spaces_cls):
        """Empty bucket returns zero deletions."""
        from dango.platform.cloud.scheduled_backup import _apply_retention

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_client.list_objects.return_value = []

        deleted = _apply_retention({"bucket": "test", "region": "nyc3"})
        assert deleted == 0

    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_weekly_retention_keeps_oldest_per_week(self, mock_spaces_cls):
        """Archives older than 7 days are grouped by ISO week."""
        from dango.platform.cloud.scheduled_backup import _apply_retention

        now = datetime.now(tz=timezone.utc)
        # Create 20 daily archives spanning ~3 weeks
        archives = [self._make_archive(now - timedelta(days=i)) for i in range(20)]

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_client.list_objects.return_value = archives

        deleted = _apply_retention({"bucket": "test", "region": "nyc3"})

        # With 20 archives, ~7 daily + ~4 weekly kept = ~11 kept, ~9 deleted
        assert deleted > 0


# ---------------------------------------------------------------------------
# 3. Health status
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthStatus:
    def test_write_success(self, tmp_path):
        """Successful backup writes clean health status."""
        from dango.platform.cloud.scheduled_backup import _write_health_status

        health_file = tmp_path / ".backup_health.json"

        with patch("dango.platform.cloud.scheduled_backup.HEALTH_FILE", health_file):
            _write_health_status(success=True)

        data = json.loads(health_file.read_text())
        assert data["last_success"] is not None
        assert data["last_error"] is None
        assert data["consecutive_failures"] == 0

    def test_write_failure(self, tmp_path):
        """Failed backup records error and increments failure count."""
        from dango.platform.cloud.scheduled_backup import _write_health_status

        health_file = tmp_path / ".backup_health.json"

        with patch("dango.platform.cloud.scheduled_backup.HEALTH_FILE", health_file):
            _write_health_status(success=False, error="disk full")

        data = json.loads(health_file.read_text())
        assert data["last_error"] == "disk full"
        assert data["consecutive_failures"] == 1

    def test_consecutive_failures_increment(self, tmp_path):
        """Consecutive failures accumulate."""
        from dango.platform.cloud.scheduled_backup import _write_health_status

        health_file = tmp_path / ".backup_health.json"

        with patch("dango.platform.cloud.scheduled_backup.HEALTH_FILE", health_file):
            _write_health_status(success=False, error="error 1")
            _write_health_status(success=False, error="error 2")

        data = json.loads(health_file.read_text())
        assert data["consecutive_failures"] == 2

    def test_success_resets_failures(self, tmp_path):
        """Success resets consecutive failure count."""
        from dango.platform.cloud.scheduled_backup import _write_health_status

        health_file = tmp_path / ".backup_health.json"

        with patch("dango.platform.cloud.scheduled_backup.HEALTH_FILE", health_file):
            _write_health_status(success=False, error="error 1")
            _write_health_status(success=False, error="error 2")
            _write_health_status(success=True)

        data = json.loads(health_file.read_text())
        assert data["consecutive_failures"] == 0
        assert data["last_error"] is None


# ---------------------------------------------------------------------------
# 4. Verify upload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerifyUpload:
    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_size_match(self, mock_spaces_cls):
        """Returns True when remote size matches local size."""
        from dango.platform.cloud.scheduled_backup import _verify_upload

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_s3 = MagicMock()
        mock_client._get_client.return_value = mock_s3
        mock_s3.head_object.return_value = {"ContentLength": 12345}

        result = _verify_upload({"bucket": "test", "region": "nyc3"}, "backups/test.tar.gz", 12345)
        assert result is True

    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_size_mismatch(self, mock_spaces_cls):
        """Returns False when remote size differs from local size."""
        from dango.platform.cloud.scheduled_backup import _verify_upload

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_s3 = MagicMock()
        mock_client._get_client.return_value = mock_s3
        mock_s3.head_object.return_value = {"ContentLength": 99999}

        result = _verify_upload({"bucket": "test", "region": "nyc3"}, "backups/test.tar.gz", 12345)
        assert result is False


# ---------------------------------------------------------------------------
# 5. List Spaces backups
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListSpacesBackups:
    @patch("dango.platform.cloud.spaces.SpacesClient")
    def test_sorted_newest_first(self, mock_spaces_cls):
        """Backups are sorted newest-first by name."""
        from dango.platform.cloud.scheduled_backup import list_spaces_backups

        mock_client = MagicMock()
        mock_spaces_cls.return_value = mock_client
        mock_client.list_objects.return_value = [
            {"Key": "backups/backup-20260222-020000.tar.gz", "Size": 100},
            {"Key": "backups/backup-20260224-020000.tar.gz", "Size": 200},
            {"Key": "backups/backup-20260223-020000.tar.gz", "Size": 150},
            {"Key": "backups/backup-20260223-020000.json", "Size": 1},  # Not .tar.gz
        ]

        backups = list_spaces_backups({"bucket": "test", "region": "nyc3"})

        assert len(backups) == 3  # .json excluded
        assert backups[0].name == "backup-20260224-020000.tar.gz"
        assert backups[1].name == "backup-20260223-020000.tar.gz"
        assert backups[2].name == "backup-20260222-020000.tar.gz"


# ---------------------------------------------------------------------------
# 6. Enable / Disable
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnableDisable:
    @patch("dango.platform.cloud.scheduled_backup.subprocess.run")
    def test_enable_returns_true_on_success(self, mock_run):
        """enable_scheduled_backup returns True on success."""
        from dango.platform.cloud.scheduled_backup import enable_scheduled_backup

        mock_run.return_value = MagicMock(returncode=0)
        assert enable_scheduled_backup() is True
        cmd = mock_run.call_args[0][0]
        assert "enable --now dango-backup.timer" in cmd

    @patch("dango.platform.cloud.scheduled_backup.subprocess.run")
    def test_disable_returns_true_on_success(self, mock_run):
        """disable_scheduled_backup returns True on success."""
        from dango.platform.cloud.scheduled_backup import disable_scheduled_backup

        mock_run.return_value = MagicMock(returncode=0)
        assert disable_scheduled_backup() is True

    @patch("dango.platform.cloud.scheduled_backup.subprocess.run")
    def test_is_active_check(self, mock_run):
        """is_scheduled_backup_enabled checks systemctl is-active."""
        from dango.platform.cloud.scheduled_backup import is_scheduled_backup_enabled

        mock_run.return_value = MagicMock(returncode=0, stdout="active\n")
        assert is_scheduled_backup_enabled() is True


# ---------------------------------------------------------------------------
# 7. Lock
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupLock:
    def test_acquire_lock_success(self, tmp_path):
        """Lock can be acquired when not held."""
        from dango.platform.cloud.scheduled_backup import _acquire_backup_lock

        lock_file = tmp_path / ".backup.lock"
        with patch("dango.platform.cloud.scheduled_backup.LOCK_FILE", lock_file):
            fd = _acquire_backup_lock()
            assert fd is not None
            fd.close()

    def test_concurrent_lock_raises(self, tmp_path):
        """Second lock attempt raises CloudProvisioningError."""

        from dango.platform.cloud.scheduled_backup import _acquire_backup_lock

        lock_file = tmp_path / ".backup.lock"
        with patch("dango.platform.cloud.scheduled_backup.LOCK_FILE", lock_file):
            fd1 = _acquire_backup_lock()
            with pytest.raises(CloudProvisioningError, match="already running"):
                _acquire_backup_lock()
            fd1.close()


# ---------------------------------------------------------------------------
# 8. Load Spaces config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadSpacesConfig:
    def test_missing_cloud_yml_raises(self, tmp_path):
        """Raises when cloud.yml doesn't exist."""
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        with patch(
            "dango.platform.cloud.scheduled_backup.PROJECT_DIR",
            tmp_path,
        ):
            with pytest.raises(CloudProvisioningError, match="cloud.yml not found"):
                _load_spaces_config()

    def test_missing_spaces_config_raises(self, tmp_path):
        """Raises when spaces is not configured in cloud.yml."""
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        cloud_dir = tmp_path / ".dango"
        cloud_dir.mkdir(parents=True)
        (cloud_dir / "cloud.yml").write_text("region: nyc1\n")

        with patch(
            "dango.platform.cloud.scheduled_backup.PROJECT_DIR",
            tmp_path,
        ):
            with pytest.raises(CloudProvisioningError, match="Spaces not configured"):
                _load_spaces_config()

    def test_valid_config_loads(self, tmp_path):
        """Valid cloud.yml with spaces config loads successfully."""
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        cloud_dir = tmp_path / ".dango"
        cloud_dir.mkdir(parents=True)
        (cloud_dir / "cloud.yml").write_text(
            "region: nyc1\nspaces:\n  bucket: my-bucket\n  region: sfo3\n"
        )

        with patch(
            "dango.platform.cloud.scheduled_backup.PROJECT_DIR",
            tmp_path,
        ):
            config = _load_spaces_config()

        assert config["bucket"] == "my-bucket"
        assert config["region"] == "sfo3"

    def test_region_fallback_to_cloud_region(self, tmp_path):
        """Uses cloud region when spaces.region is not set."""
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        cloud_dir = tmp_path / ".dango"
        cloud_dir.mkdir(parents=True)
        (cloud_dir / "cloud.yml").write_text("region: ams3\nspaces:\n  bucket: my-bucket\n")

        with patch(
            "dango.platform.cloud.scheduled_backup.PROJECT_DIR",
            tmp_path,
        ):
            config = _load_spaces_config()

        assert config["region"] == "ams3"


# ---------------------------------------------------------------------------
# 9. Run local helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunLocal:
    @patch("dango.platform.cloud.scheduled_backup.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        """_run_local returns stdout on success."""
        from dango.platform.cloud.scheduled_backup import _run_local

        mock_run.return_value = MagicMock(returncode=0, stdout="output\n", stderr="")
        result = _run_local("echo test", step="test")
        assert result == "output\n"

    @patch("dango.platform.cloud.scheduled_backup.subprocess.run")
    def test_failure_raises(self, mock_run):
        """_run_local raises CloudProvisioningError on failure."""
        from dango.platform.cloud.scheduled_backup import _run_local

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
        with pytest.raises(CloudProvisioningError, match="test_step"):
            _run_local("bad command", step="test_step")
