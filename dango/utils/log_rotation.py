"""dango/utils/log_rotation.py

JSONL log rotation with gzip compression and retention management.

Rotates audit.jsonl and activity.jsonl files based on size (>5 MB) or age
(>1 day). Archives are gzip-compressed with YYYYMMDD date suffixes.
All public functions follow the never-fail contract — errors are logged
as warnings, never raised.
"""

from __future__ import annotations

import gzip
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger

_logger = get_logger(__name__)

# Rotation thresholds
_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
_MAX_FILE_AGE_SECONDS = 86_400  # 1 day


def rotate_jsonl_log(log_path: Path, max_age_days: int = 90) -> None:
    """Rotate a JSONL log file if size or age thresholds are exceeded.

    Rotation is triggered when the file exceeds 5 MB or is older than 1 day
    (whichever comes first). The rotated file is gzip-compressed to
    ``{stem}.YYYYMMDD.jsonl.gz``. Archives older than *max_age_days* are
    deleted automatically.

    Never raises — all errors are caught and logged as warnings.

    Args:
        log_path: Path to the JSONL file to rotate.
        max_age_days: Delete archives older than this many days.
    """
    try:
        _rotate_jsonl_log_impl(log_path, max_age_days)
    except Exception:
        _logger.warning("log_rotation_failed", log_path=str(log_path), exc_info=True)


def cleanup_old_archives(log_dir: Path, pattern: str, max_age_days: int = 90) -> None:
    """Delete archived log files older than *max_age_days*.

    Never raises — all errors are caught and logged as warnings.

    Args:
        log_dir: Directory containing archives.
        pattern: Glob pattern for archive files (e.g. ``"audit.*.jsonl.gz"``).
        max_age_days: Maximum age in days before deletion.
    """
    try:
        _cleanup_old_archives_impl(log_dir, pattern, max_age_days)
    except Exception:
        _logger.warning("archive_cleanup_failed", log_dir=str(log_dir), exc_info=True)


def get_log_disk_usage(log_dir: Path) -> dict[str, Any]:
    """Return per-file sizes and total disk usage for a log directory.

    Never raises — returns an empty result on error.

    Args:
        log_dir: Directory to scan.

    Returns:
        Dict with ``"files"`` (mapping of filename to size in bytes)
        and ``"total_bytes"`` (int).
    """
    result: dict[str, Any] = {"files": {}, "total_bytes": 0}

    try:
        if not log_dir.exists():
            return result

        for entry in log_dir.iterdir():
            if entry.is_file():
                size = entry.stat().st_size
                result["files"][entry.name] = size
                result["total_bytes"] += size
    except Exception:
        _logger.warning("disk_usage_scan_failed", log_dir=str(log_dir), exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rotate_jsonl_log_impl(log_path: Path, max_age_days: int) -> None:
    """Core rotation logic — may raise."""
    if not log_path.exists():
        return

    stat = log_path.stat()

    # Empty files don't need rotation
    if stat.st_size == 0:
        cleanup_old_archives(log_path.parent, f"{log_path.stem}.*.jsonl.gz", max_age_days)
        return

    # Check triggers: size > 5 MB or mtime > 1 day old
    now_ts = datetime.now(timezone.utc).timestamp()
    age_seconds = now_ts - stat.st_mtime
    needs_rotation = stat.st_size > _MAX_FILE_SIZE or age_seconds > _MAX_FILE_AGE_SECONDS

    if not needs_rotation:
        cleanup_old_archives(log_path.parent, f"{log_path.stem}.*.jsonl.gz", max_age_days)
        return

    # Determine archive name with same-day collision handling
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    stem = log_path.stem  # e.g. "audit" from "audit.jsonl"
    archive_name = f"{stem}.{date_str}.jsonl.gz"
    archive_path = log_path.parent / archive_name

    counter = 0
    while archive_path.exists():
        counter += 1
        archive_name = f"{stem}.{date_str}_{counter}.jsonl.gz"
        archive_path = log_path.parent / archive_name

    # Rotate: atomic rename → touch new → compress → delete temp
    tmp_path = log_path.parent / (log_path.name + ".rotating")

    # Clean up any leftover temp file from a previous failed rotation
    if tmp_path.exists():
        tmp_path.unlink()

    os.rename(log_path, tmp_path)

    # Create new empty file so writers can continue immediately
    log_path.touch()

    # Compress the renamed file into the archive.
    # On failure, restore the original so data is not stranded in .rotating.
    try:
        with open(tmp_path, "rb") as f_in:
            with gzip.open(archive_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        tmp_path.unlink()
    except Exception:
        # Remove partial archive if it was created
        if archive_path.exists():
            archive_path.unlink()
        # Restore: prepend old data back into the (now-empty) live file
        if tmp_path.exists():
            new_data = log_path.read_bytes()
            old_data = tmp_path.read_bytes()
            log_path.write_bytes(old_data + new_data)
            tmp_path.unlink()
        raise

    _logger.info(
        "log_rotated",
        log_path=str(log_path),
        archive=str(archive_path),
        original_size=stat.st_size,
    )

    # Clean up old archives
    cleanup_old_archives(log_path.parent, f"{stem}.*.jsonl.gz", max_age_days)


def _cleanup_old_archives_impl(log_dir: Path, pattern: str, max_age_days: int) -> None:
    """Core cleanup logic — may raise."""
    if not log_dir.exists():
        return

    cutoff_ts = datetime.now(timezone.utc).timestamp() - (max_age_days * 86_400)

    for archive in log_dir.glob(pattern):
        if archive.stat().st_mtime < cutoff_ts:
            archive.unlink()
            _logger.info("archive_deleted", path=str(archive))
