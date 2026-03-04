"""tests/unit/test_log_rotation.py

Tests for dango.utils.log_rotation — JSONL log rotation, archive cleanup,
and disk usage reporting.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.utils.log_rotation import (
    _MAX_FILE_AGE_SECONDS,
    _MAX_FILE_SIZE,
    cleanup_old_archives,
    get_log_disk_usage,
    rotate_jsonl_log,
)


def _write_jsonl(path: Path, num_bytes: int) -> str:
    """Write JSONL data to *path* totalling approximately *num_bytes*."""
    line = json.dumps({"event": "test", "data": "x" * 80}) + "\n"
    count = max(1, num_bytes // len(line))
    content = line * count
    path.write_text(content)
    return content


@pytest.mark.unit
class TestRotateJsonlLog:
    def test_rotates_on_size(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)

        rotate_jsonl_log(log)

        # Original should be empty (newly created)
        assert log.exists()
        assert log.stat().st_size == 0

        # Archive should exist and be gzip-compressed
        archives = list(tmp_path.glob("audit.*.jsonl.gz"))
        assert len(archives) == 1
        assert gzip.decompress(archives[0].read_bytes())

    def test_rotates_on_age(self, tmp_path: Path) -> None:
        log = tmp_path / "activity.jsonl"
        log.write_text('{"event":"old"}\n')

        # Set mtime to 2 days ago
        old_time = time.time() - (_MAX_FILE_AGE_SECONDS + 3600)
        os.utime(log, (old_time, old_time))

        rotate_jsonl_log(log)

        assert log.exists()
        assert log.stat().st_size == 0
        archives = list(tmp_path.glob("activity.*.jsonl.gz"))
        assert len(archives) == 1

    def test_no_rotation_when_thresholds_not_met(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        log.write_text('{"event":"recent"}\n')
        original_size = log.stat().st_size

        rotate_jsonl_log(log)

        # File should be unchanged
        assert log.stat().st_size == original_size
        archives = list(tmp_path.glob("audit.*.jsonl.gz"))
        assert len(archives) == 0

    def test_skips_empty_file(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        log.touch()

        # Set mtime to 2 days ago — still should not rotate empty file
        old_time = time.time() - (_MAX_FILE_AGE_SECONDS + 3600)
        os.utime(log, (old_time, old_time))

        rotate_jsonl_log(log)

        archives = list(tmp_path.glob("audit.*.jsonl.gz"))
        assert len(archives) == 0

    def test_nonexistent_file_is_noop(self, tmp_path: Path) -> None:
        log = tmp_path / "missing.jsonl"
        rotate_jsonl_log(log)  # should not raise

    def test_archive_content_matches_original(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        content = _write_jsonl(log, _MAX_FILE_SIZE + 1024)

        rotate_jsonl_log(log)

        archives = list(tmp_path.glob("audit.*.jsonl.gz"))
        decompressed = gzip.decompress(archives[0].read_bytes()).decode()
        assert decompressed == content

    def test_same_day_collision_appends_counter(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"

        # First rotation
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)
        rotate_jsonl_log(log)

        # Second rotation — same day
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)
        rotate_jsonl_log(log)

        archives = sorted(tmp_path.glob("audit.*.jsonl.gz"))
        assert len(archives) == 2
        # Second archive should have _1 suffix
        assert "_1" in archives[1].name

    def test_archive_naming_format(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)

        rotate_jsonl_log(log)

        archives = list(tmp_path.glob("audit.*.jsonl.gz"))
        assert len(archives) == 1
        name = archives[0].name
        # Format: audit.YYYYMMDD.jsonl.gz
        assert name.startswith("audit.")
        assert name.endswith(".jsonl.gz")
        date_part = name.replace("audit.", "").replace(".jsonl.gz", "")
        assert len(date_part) == 8
        assert date_part.isdigit()

    def test_never_raises_on_rename_error(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)

        with patch("dango.utils.log_rotation.os.rename", side_effect=OSError("perm")):
            rotate_jsonl_log(log)  # should not raise

        # Original file should still exist
        assert log.exists()

    def test_cleans_up_leftover_rotating_file(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        leftover = tmp_path / "audit.jsonl.rotating"
        leftover.write_text("leftover")
        _write_jsonl(log, _MAX_FILE_SIZE + 1024)

        rotate_jsonl_log(log)

        assert not leftover.exists()


@pytest.mark.unit
class TestCleanupOldArchives:
    def test_deletes_old_archives(self, tmp_path: Path) -> None:
        old_archive = tmp_path / "audit.20250101.jsonl.gz"
        old_archive.write_bytes(b"old")
        old_time = time.time() - (91 * 86_400)  # 91 days ago
        os.utime(old_archive, (old_time, old_time))

        cleanup_old_archives(tmp_path, "audit.*.jsonl.gz", max_age_days=90)

        assert not old_archive.exists()

    def test_keeps_recent_archives(self, tmp_path: Path) -> None:
        recent = tmp_path / "audit.20260301.jsonl.gz"
        recent.write_bytes(b"recent")

        cleanup_old_archives(tmp_path, "audit.*.jsonl.gz", max_age_days=90)

        assert recent.exists()

    def test_never_raises_on_error(self, tmp_path: Path) -> None:
        with patch(
            "dango.utils.log_rotation._cleanup_old_archives_impl",
            side_effect=OSError("fail"),
        ):
            cleanup_old_archives(tmp_path, "*.gz")  # should not raise

    def test_noop_on_missing_directory(self) -> None:
        cleanup_old_archives(Path("/nonexistent/dir"), "*.gz")


@pytest.mark.unit
class TestGetLogDiskUsage:
    def test_returns_per_file_sizes(self, tmp_path: Path) -> None:
        (tmp_path / "dango.log").write_text("x" * 100)
        (tmp_path / "audit.jsonl").write_text("y" * 200)

        result = get_log_disk_usage(tmp_path)

        assert "dango.log" in result["files"]
        assert "audit.jsonl" in result["files"]
        assert result["files"]["dango.log"] == 100
        assert result["files"]["audit.jsonl"] == 200
        assert result["total_bytes"] == 300

    def test_returns_empty_for_missing_directory(self) -> None:
        result = get_log_disk_usage(Path("/nonexistent/dir"))

        assert result["files"] == {}
        assert result["total_bytes"] == 0

    def test_includes_archives(self, tmp_path: Path) -> None:
        (tmp_path / "audit.20260301.jsonl.gz").write_bytes(b"compressed")

        result = get_log_disk_usage(tmp_path)

        assert "audit.20260301.jsonl.gz" in result["files"]
        assert result["total_bytes"] > 0

    def test_never_raises_on_error(self, tmp_path: Path) -> None:
        with patch("dango.utils.log_rotation.Path.iterdir", side_effect=OSError("fail")):
            result = get_log_disk_usage(tmp_path)

        assert result["total_bytes"] == 0
