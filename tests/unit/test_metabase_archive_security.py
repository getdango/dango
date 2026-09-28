"""tests/unit/test_metabase_archive_security.py

Regression coverage for password-free Metabase backup and restore paths.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_METABASE_YAML = """url: http://localhost:3000
database_id: 7
admin:
  email: admin@example.com
  password: should-never-be-archived
"""


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.mark.unit
class TestMetabaseArchiveSanitization:
    def test_sanitized_metadata_retains_non_secret_fields(self, tmp_path):
        """Sanitization keeps connection metadata while removing admin.password."""
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        source = tmp_path / "metabase.yml"
        source.write_text(_METABASE_YAML)

        sanitized = yaml.safe_load(_sanitized_metabase_yaml(source))

        assert sanitized == {
            "url": "http://localhost:3000",
            "database_id": 7,
            "admin": {"email": "admin@example.com"},
        }

    def test_local_safety_archive_excludes_admin_password(self, tmp_path):
        """Pre-restore archives keep password-free Metabase metadata only."""
        from dango.cli.commands.local_backup import _create_safety_backup

        config = tmp_path / ".dango" / "metabase.yml"
        config.parent.mkdir()
        config.write_text(_METABASE_YAML)

        safety_archive = _create_safety_backup(tmp_path)

        assert safety_archive is not None
        with tarfile.open(safety_archive, "r:gz") as archive:
            restored = yaml.safe_load(archive.extractfile(".dango/metabase.yml").read())
        assert restored["admin"] == {"email": "admin@example.com"}

    def test_local_restore_sanitizes_historical_archive(self, tmp_path, capsys):
        """Restoring a legacy archive never recreates a plaintext password."""
        from dango.cli.commands.local_backup import _extract_archive

        archive_path = tmp_path / "legacy.tar.gz"
        _write_archive(archive_path, {".dango/metabase.yml": _METABASE_YAML.encode()})

        _extract_archive(archive_path, tmp_path / "project")

        restored = yaml.safe_load((tmp_path / "project/.dango/metabase.yml").read_text())
        assert restored["admin"] == {"email": "admin@example.com"}
        assert "should-never-be-archived" not in capsys.readouterr().out

    def test_local_restore_omits_malformed_metabase_yaml(self, tmp_path, capsys):
        """Malformed archived YAML is skipped and emits a non-secret warning."""
        from dango.cli.commands.local_backup import _extract_archive

        archive_path = tmp_path / "legacy.tar.gz"
        _write_archive(archive_path, {".dango/metabase.yml": b"admin: [not-valid"})

        _extract_archive(archive_path, tmp_path / "project")

        assert not (tmp_path / "project/.dango/metabase.yml").exists()
        assert "Metabase metadata omitted" in capsys.readouterr().out

    def test_scheduled_archive_excludes_admin_password(self, tmp_path):
        """Scheduled Spaces archives contain only sanitized Metabase metadata."""
        from dango.platform.cloud.scheduled_backup import _create_local_archive

        project_dir = tmp_path / "project"
        config = project_dir / ".dango" / "metabase.yml"
        config.parent.mkdir(parents=True)
        config.write_text(_METABASE_YAML)
        backup_dir = tmp_path / "backups"

        with (
            patch("dango.platform.cloud.scheduled_backup.PROJECT_DIR", project_dir),
            patch("dango.platform.cloud.scheduled_backup.BACKUP_DIR", backup_dir),
            patch("dango.platform.cloud.scheduled_backup._stop_services"),
            patch("dango.platform.cloud.scheduled_backup._start_services"),
            patch("dango.platform.cloud.scheduled_backup._checkpoint_databases", return_value=[]),
            patch(
                "dango.platform.cloud.scheduled_backup._get_metabase_volume_path", return_value=None
            ),
        ):
            archive_path, manifest, _warnings = _create_local_archive("scheduled")

        assert ".dango/metabase.yml" in [entry["path"] for entry in manifest["files"]]
        with tarfile.open(archive_path, "r:gz") as archive:
            member = next(
                name for name in archive.getnames() if name.endswith("/.dango/metabase.yml")
            )
            restored = yaml.safe_load(archive.extractfile(member).read())
        assert restored["admin"] == {"email": "admin@example.com"}

    def test_remote_archive_and_restore_use_sanitizer(self):
        """Cloud archive creation and historical restore both sanitize metadata."""
        from dango.platform.cloud.backup import _create_archive, restore_from_archive

        ssh = MagicMock()
        ssh.exec_command.return_value = MagicMock(success=True, stdout="", stderr="", exit_code=0)

        with (
            patch("dango.platform.cloud.backup._run_checked", return_value=""),
            patch(
                "dango.platform.cloud.backup._sanitize_remote_metabase_yaml", return_value=None
            ) as sanitize,
        ):
            _create_archive(ssh, "20260929-010101", None, "test")
            sanitize.assert_called_once_with(
                ssh,
                "/srv/dango/project/.dango/metabase.yml",
                "/tmp/backup-20260929-010101/.dango/metabase.yml",
            )

        ssh.reset_mock()
        ssh.exec_command.return_value = MagicMock(
            success=True, stdout='{"timestamp": "test"}', stderr="", exit_code=0
        )
        with (
            patch("dango.platform.cloud.backup.create_backup") as create_backup,
            patch("dango.platform.cloud.backup._run_checked", return_value=""),
            patch(
                "dango.platform.cloud.backup._sanitize_remote_metabase_yaml", return_value=None
            ) as sanitize,
            patch("dango.platform.cloud.backup._get_metabase_volume_path", return_value=None),
            patch("dango.platform.cloud.backup.verify_health", return_value=True),
        ):
            create_backup.return_value.archive_path = "/srv/dango/backups/deploy/safety.tar.gz"
            restore_from_archive(ssh, "/srv/dango/backups/deploy/backup-20260929-010101.tar.gz")
            sanitize.assert_called_once_with(
                ssh,
                "/tmp/backup-20260929-010101/.dango/metabase.yml",
                "/srv/dango/project/.dango/metabase.yml",
            )

    def test_remote_malformed_yaml_warning_never_logs_parser_content(self):
        """Remote sanitization errors report only a generic, non-secret warning."""
        from dango.platform.cloud.backup import _sanitize_remote_metabase_yaml

        ssh = MagicMock()
        ssh.exec_command.return_value = MagicMock(
            success=False,
            stderr="while parsing password: should-never-be-logged",
        )
        with patch("dango.platform.cloud.backup._logger.warning") as warning:
            result = _sanitize_remote_metabase_yaml(
                ssh, "/source/.dango/metabase.yml", "/archive/.dango/metabase.yml"
            )

        assert result == "Metabase metadata omitted because its YAML could not be safely sanitized"
        warning.assert_called_once_with("metabase_backup_metadata_omitted")
