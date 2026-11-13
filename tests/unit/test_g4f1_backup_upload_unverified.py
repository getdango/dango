"""tests/unit/test_g4f1_backup_upload_unverified.py

I8-09: an upload that could not be verified is a failed backup: the local archive and its
manifest stay, retention does not run, and the health file records the failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_SB = "dango.platform.cloud.scheduled_backup"
_NAME = "backup-20260224-143000"


def _run(tmp_path: Path, *, verified: bool) -> tuple[Any, MagicMock, MagicMock, Path, Path]:
    from dango.platform.cloud.scheduled_backup import run_scheduled_backup

    archive = tmp_path / f"{_NAME}.tar.gz"
    archive.write_text("archive")
    manifest = tmp_path / f"{_NAME}.json"
    manifest.write_text("{}")
    with (
        patch(f"{_SB}._load_backup_config", return_value={}),
        patch(f"{_SB}._load_spaces_config", return_value={"bucket": "b", "region": "nyc3"}),
        patch(f"{_SB}._create_local_archive", return_value=(archive, {}, [])),
        patch(f"{_SB}._acquire_backup_lock", return_value=MagicMock()),
        patch(f"{_SB}._check_spaces_reuse"),
        patch(f"{_SB}._upload_to_spaces", return_value=f"backups/{_NAME}.tar.gz"),
        patch(f"{_SB}._verify_upload", return_value=verified),
        patch(f"{_SB}._apply_retention", return_value=2) as retention,
        patch(f"{_SB}._write_health_status") as health,
        patch(f"{_SB}.fcntl.flock"),
        patch(f"{_SB}.BACKUP_DIR", tmp_path),
    ):
        result = run_scheduled_backup()
    return result, retention, health, archive, manifest


@pytest.mark.unit
class TestUnverifiedUploadIsAFailure:
    def test_keeps_local_archive_and_manifest(self, tmp_path: Path) -> None:
        _result, _retention, _health, archive, manifest = _run(tmp_path, verified=False)
        assert archive.exists() and manifest.exists()

    def test_runs_no_retention(self, tmp_path: Path) -> None:
        _result, retention, _health, _archive, _manifest = _run(tmp_path, verified=False)
        retention.assert_not_called()

    def test_records_unhealthy_status_and_error(self, tmp_path: Path) -> None:
        result, _retention, health, _archive, _manifest = _run(tmp_path, verified=False)
        assert result.error and "verif" in result.error.lower()
        assert result.off_site is False
        health.assert_called_once()
        assert health.call_args.kwargs["success"] is False
        assert health.call_args.kwargs["error"]


@pytest.mark.unit
class TestVerifiedUploadCleansUpBothFiles:
    def test_removes_archive_and_the_manifest_with_the_right_name(self, tmp_path: Path) -> None:
        result, retention, health, archive, manifest = _run(tmp_path, verified=True)
        assert result.error is None
        assert not archive.exists()
        assert not manifest.exists(), "the manifest is <name>.json, not <name>.tar.json"
        retention.assert_called_once()
        assert health.call_args.kwargs["success"] is True
