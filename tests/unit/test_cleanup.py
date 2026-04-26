"""tests/unit/test_cleanup.py

Unit tests for the ``dango cleanup`` CLI command.
"""

from __future__ import annotations

import gzip
import os
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.cli.commands.cleanup import (
    _collect_dbt_artifacts,
    _collect_docker_volumes,
    _collect_log_archives,
    _collect_pycache,
    _format_size,
    cleanup,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Patch targets (lazy imports inside the command function body)
# ---------------------------------------------------------------------------
_CMD = "dango.cli.commands.cleanup"
_UTILS = "dango.cli.utils"
_DB_HEALTH = "dango.utils.db_health"
_LOG_ROT = "dango.utils.log_rotation"


def _disk_summary(**overrides: object) -> dict:
    defaults = {"free_gb": 50.0, "total_gb": 100.0, "used_gb": 50.0, "status": "healthy"}
    defaults.update(overrides)
    return defaults


def _log_usage(total_bytes: int = 0) -> dict:
    return {"files": {}, "total_bytes": total_bytes}


def _make_old_archive(log_dir: Path, name: str, size: int = 1024, age_days: int = 120) -> Path:
    """Create a fake .jsonl.gz archive with an old mtime."""
    archive = log_dir / name
    archive.write_bytes(gzip.compress(b"x" * size))
    old_ts = time.time() - (age_days * 86_400)
    os.utime(archive, (old_ts, old_ts))
    return archive


def _make_recent_archive(log_dir: Path, name: str, size: int = 512) -> Path:
    archive = log_dir / name
    archive.write_bytes(gzip.compress(b"y" * size))
    return archive


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(0) == "0 B"
        assert _format_size(512) == "512 B"

    def test_kilobytes(self) -> None:
        assert _format_size(1024) == "1.0 KB"
        assert _format_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert _format_size(1024**2) == "1.0 MB"

    def test_gigabytes(self) -> None:
        assert _format_size(1024**3) == "1.00 GB"


# ---------------------------------------------------------------------------
# _collect_log_archives
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestCollectLogArchives:
    def test_empty_dir(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        assert _collect_log_archives(log_dir, max_age_days=90) == []

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert _collect_log_archives(tmp_path / "nope", max_age_days=90) == []

    def test_finds_old_archives(self, tmp_path: Path) -> None:
        _make_old_archive(tmp_path, "audit.2025-01-01.jsonl.gz", age_days=120)
        _make_recent_archive(tmp_path, "audit.2026-03-01.jsonl.gz")

        results = _collect_log_archives(tmp_path, max_age_days=90)
        assert len(results) == 1
        assert results[0][0].name == "audit.2025-01-01.jsonl.gz"

    def test_ignores_non_gz(self, tmp_path: Path) -> None:
        old = tmp_path / "audit.jsonl"
        old.write_text("data")
        old_ts = time.time() - (120 * 86_400)
        os.utime(old, (old_ts, old_ts))

        assert _collect_log_archives(tmp_path, max_age_days=90) == []


# ---------------------------------------------------------------------------
# _collect_dbt_artifacts
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestCollectDbtArtifacts:
    def test_no_dbt_dir(self, tmp_path: Path) -> None:
        assert _collect_dbt_artifacts(tmp_path) == []

    def test_finds_target_and_logs(self, tmp_path: Path) -> None:
        for subdir in ("target", "logs"):
            d = tmp_path / "dbt" / subdir
            d.mkdir(parents=True)
            (d / "file.txt").write_text("data")

        results = _collect_dbt_artifacts(tmp_path)
        assert len(results) == 2
        names = {p.name for p, _ in results}
        assert names == {"target", "logs"}

    def test_calculates_size(self, tmp_path: Path) -> None:
        target = tmp_path / "dbt" / "target"
        target.mkdir(parents=True)
        (target / "a.json").write_text("a" * 100)
        (target / "b.json").write_text("b" * 200)

        results = _collect_dbt_artifacts(tmp_path)
        assert len(results) == 1
        assert results[0][1] == 300


# ---------------------------------------------------------------------------
# _collect_pycache
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestCollectPycache:
    def test_finds_pycache(self, tmp_path: Path) -> None:
        cache = tmp_path / "pkg" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.pyc").write_bytes(b"\x00" * 64)

        results = _collect_pycache(tmp_path)
        assert len(results) == 1
        assert results[0][1] == 64

    def test_skips_venv(self, tmp_path: Path) -> None:
        cache = tmp_path / "venv" / "lib" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.pyc").write_bytes(b"\x00" * 64)

        assert _collect_pycache(tmp_path) == []

    def test_skips_dot_venv(self, tmp_path: Path) -> None:
        cache = tmp_path / ".venv" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.pyc").write_bytes(b"\x00")

        assert _collect_pycache(tmp_path) == []

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        cache = tmp_path / "node_modules" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.pyc").write_bytes(b"\x00")

        assert _collect_pycache(tmp_path) == []

    def test_skips_dot_git(self, tmp_path: Path) -> None:
        cache = tmp_path / ".git" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.pyc").write_bytes(b"\x00")

        assert _collect_pycache(tmp_path) == []

    def test_no_double_counting_nested(self, tmp_path: Path) -> None:
        """Nested __pycache__ inside __pycache__ should not be double-counted."""
        outer = tmp_path / "pkg" / "__pycache__"
        inner = outer / "sub" / "__pycache__"
        inner.mkdir(parents=True)
        (outer / "a.pyc").write_bytes(b"\x00" * 10)
        (inner / "b.pyc").write_bytes(b"\x00" * 20)

        results = _collect_pycache(tmp_path)
        # Only the outer cache should appear; inner is nested within it.
        assert len(results) == 1
        assert results[0][0] == outer
        # Size should include all files under outer (including nested)
        assert results[0][1] == 30

    def test_empty_project(self, tmp_path: Path) -> None:
        assert _collect_pycache(tmp_path) == []


# ---------------------------------------------------------------------------
# cleanup command (Click integration)
# ---------------------------------------------------------------------------
def _invoke_cleanup(
    tmp_path: Path,
    args: list[str] | None = None,
    log_archives: list[tuple[Path, int]] | None = None,
    dbt_artifacts: list[tuple[Path, int]] | None = None,
    pycache_dirs: list[tuple[Path, int]] | None = None,
    disk_before: dict[str, object] | None = None,
    disk_after: dict[str, object] | None = None,
    log_before: int = 5000,
    log_after: int = 0,
) -> click.testing.Result:
    """Invoke the cleanup command with standard mocks."""
    runner = CliRunner()

    if log_archives is None:
        log_archives = []
    if dbt_artifacts is None:
        dbt_artifacts = []
    if pycache_dirs is None:
        pycache_dirs = []
    if disk_before is None:
        disk_before = _disk_summary(used_gb=55.0)
    if disk_after is None:
        disk_after = _disk_summary(used_gb=54.9)

    log_usage_calls = [_log_usage(log_before), _log_usage(log_after)]
    # During cleanup, log usage may be called extra times for actual measurement
    # Pad with final value to avoid StopIteration
    log_side = log_usage_calls + [_log_usage(log_after)] * 5

    with (
        patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
        patch(f"{_DB_HEALTH}.get_disk_usage_summary", side_effect=[disk_before, disk_after]),
        patch(f"{_LOG_ROT}.get_log_disk_usage", side_effect=log_side),
        patch(f"{_LOG_ROT}.cleanup_old_archives"),
        patch(f"{_CMD}._collect_log_archives", return_value=log_archives),
        patch(f"{_CMD}._collect_dbt_artifacts", return_value=dbt_artifacts),
        patch(f"{_CMD}._collect_pycache", return_value=pycache_dirs),
    ):
        result = runner.invoke(cleanup, args or [], obj={"project_root": str(tmp_path)})

    return result


@pytest.mark.unit
class TestCleanupCommand:
    def test_nothing_to_clean(self, tmp_path: Path) -> None:
        result = _invoke_cleanup(tmp_path)
        assert result.exit_code == 0
        assert "Nothing to clean up" in _strip(result.output)

    def test_dry_run_shows_items(self, tmp_path: Path) -> None:
        archive = tmp_path / ".dango" / "logs" / "audit.2025-01-01.jsonl.gz"
        result = _invoke_cleanup(
            tmp_path,
            args=["--dry-run"],
            log_archives=[(archive, 2048)],
        )
        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Dry run" in out
        assert "Log archives" in out

    def test_dry_run_no_deletion(self, tmp_path: Path) -> None:
        """--dry-run should not call cleanup_old_archives."""
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        runner = CliRunner()

        mock_cleanup = MagicMock()
        with (
            patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
            patch(f"{_DB_HEALTH}.get_disk_usage_summary", return_value=_disk_summary()),
            patch(f"{_LOG_ROT}.get_log_disk_usage", return_value=_log_usage(1000)),
            patch(f"{_LOG_ROT}.cleanup_old_archives", mock_cleanup),
            patch(f"{_CMD}._collect_log_archives", return_value=[(archive, 1000)]),
            patch(f"{_CMD}._collect_dbt_artifacts", return_value=[]),
            patch(f"{_CMD}._collect_pycache", return_value=[]),
        ):
            result = runner.invoke(cleanup, ["--dry-run"], obj={"project_root": str(tmp_path)})

        assert result.exit_code == 0
        mock_cleanup.assert_not_called()

    def test_yes_skips_confirmation(self, tmp_path: Path) -> None:
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        result = _invoke_cleanup(
            tmp_path,
            args=["--yes"],
            log_archives=[(archive, 500)],
        )
        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Removed" in out
        assert "Freed" in out

    def test_logs_only_skips_dbt_and_cache(self, tmp_path: Path) -> None:
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        runner = CliRunner()

        mock_dbt = MagicMock(return_value=[])
        mock_cache = MagicMock(return_value=[])

        with (
            patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
            patch(f"{_DB_HEALTH}.get_disk_usage_summary", return_value=_disk_summary()),
            patch(f"{_LOG_ROT}.get_log_disk_usage", return_value=_log_usage(500)),
            patch(f"{_LOG_ROT}.cleanup_old_archives"),
            patch(f"{_CMD}._collect_log_archives", return_value=[(archive, 500)]),
            patch(f"{_CMD}._collect_dbt_artifacts", mock_dbt),
            patch(f"{_CMD}._collect_pycache", mock_cache),
        ):
            result = runner.invoke(
                cleanup, ["--logs-only", "--yes"], obj={"project_root": str(tmp_path)}
            )

        assert result.exit_code == 0
        mock_dbt.assert_not_called()
        mock_cache.assert_not_called()

    def test_summary_table_shows_all_categories(self, tmp_path: Path) -> None:
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        dbt_dir = tmp_path / "dbt" / "target"
        cache_dir = tmp_path / "pkg" / "__pycache__"

        result = _invoke_cleanup(
            tmp_path,
            args=["--dry-run"],
            log_archives=[(archive, 1024)],
            dbt_artifacts=[(dbt_dir, 2048)],
            pycache_dirs=[(cache_dir, 512)],
        )
        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Log archives" in out
        assert "dbt artifacts" in out
        assert "Python cache" in out
        assert "Total" in out

    def test_cancelled_by_user(self, tmp_path: Path) -> None:
        """User answering 'n' to confirmation should cancel."""
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        runner = CliRunner()

        with (
            patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
            patch(f"{_DB_HEALTH}.get_disk_usage_summary", return_value=_disk_summary()),
            patch(f"{_LOG_ROT}.get_log_disk_usage", return_value=_log_usage(1000)),
            patch(f"{_LOG_ROT}.cleanup_old_archives"),
            patch(f"{_CMD}._collect_log_archives", return_value=[(archive, 1000)]),
            patch(f"{_CMD}._collect_dbt_artifacts", return_value=[]),
            patch(f"{_CMD}._collect_pycache", return_value=[]),
        ):
            result = runner.invoke(cleanup, [], input="n\n", obj={"project_root": str(tmp_path)})

        assert result.exit_code == 0
        assert "Cancelled" in _strip(result.output)

    def test_dbt_removal_failure_reported(self, tmp_path: Path) -> None:
        """OSError during dbt rmtree is reported but doesn't abort."""
        dbt_dir = tmp_path / "dbt" / "target"
        runner = CliRunner()

        with (
            patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
            patch(f"{_DB_HEALTH}.get_disk_usage_summary", return_value=_disk_summary()),
            patch(f"{_LOG_ROT}.get_log_disk_usage", return_value=_log_usage(0)),
            patch(f"{_LOG_ROT}.cleanup_old_archives"),
            patch(f"{_CMD}._collect_log_archives", return_value=[]),
            patch(f"{_CMD}._collect_dbt_artifacts", return_value=[(dbt_dir, 4096)]),
            patch(f"{_CMD}._collect_pycache", return_value=[]),
            patch("shutil.rmtree", side_effect=OSError("Permission denied")),
        ):
            result = runner.invoke(cleanup, ["--yes"], obj={"project_root": str(tmp_path)})

        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Failed to remove" in out

    def test_pycache_already_removed_skipped(self, tmp_path: Path) -> None:
        """Cache dirs that no longer exist at deletion time are skipped."""
        ghost = tmp_path / "pkg" / "__pycache__"
        # ghost doesn't exist on disk — simulates parent already removed it

        result = _invoke_cleanup(
            tmp_path,
            args=["--yes"],
            pycache_dirs=[(ghost, 256)],
        )
        assert result.exit_code == 0
        # Should not report any cache removed (ghost didn't exist)
        out = _strip(result.output)
        assert "__pycache__" not in out

    def test_freed_size_in_output(self, tmp_path: Path) -> None:
        archive = tmp_path / ".dango" / "logs" / "old.jsonl.gz"
        result = _invoke_cleanup(
            tmp_path,
            args=["--yes"],
            log_archives=[(archive, 2048)],
            log_before=5000,
            log_after=3000,
        )
        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Freed" in out


# ---------------------------------------------------------------------------
# _collect_docker_volumes
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestCollectDockerVolumes:
    def test_none_dangling(self) -> None:
        """Empty stdout returns empty list."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _collect_docker_volumes() == []

    def test_with_results(self) -> None:
        """Multiline stdout returns list of volume names."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="vol_a\nvol_b\nvol_c\n")
            result = _collect_docker_volumes()
            assert result == ["vol_a", "vol_b", "vol_c"]

    def test_docker_not_found(self) -> None:
        """FileNotFoundError returns empty list."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("docker not found")
            assert _collect_docker_volumes() == []

    def test_timeout(self) -> None:
        """TimeoutExpired returns empty list."""
        import subprocess

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("docker", 10)
            assert _collect_docker_volumes() == []


@pytest.mark.unit
class TestCleanupDockerFlag:
    def test_docker_flag_prunes(self, tmp_path: Path) -> None:
        """--docker flag collects and prunes dangling volumes."""
        runner = CliRunner()

        log_side = [_log_usage(5000), _log_usage(0)] + [_log_usage(0)] * 5

        with (
            patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
            patch(f"{_DB_HEALTH}.get_disk_usage_summary", return_value=_disk_summary()),
            patch(f"{_LOG_ROT}.get_log_disk_usage", side_effect=log_side),
            patch(f"{_LOG_ROT}.cleanup_old_archives"),
            patch(f"{_CMD}._collect_log_archives", return_value=[]),
            patch(f"{_CMD}._collect_dbt_artifacts", return_value=[]),
            patch(f"{_CMD}._collect_pycache", return_value=[]),
            patch(f"{_CMD}._collect_docker_volumes", return_value=["vol1", "vol2"]),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = runner.invoke(
                cleanup, ["--yes", "--docker"], obj={"project_root": str(tmp_path)}
            )

        assert result.exit_code == 0
        out = _strip(result.output)
        assert "Docker volumes" in out
        assert "Removed 2" in out
        # Verify docker volume rm called for each volume, not prune
        rm_calls = [c for c in mock_run.call_args_list if "volume" in str(c) and "rm" in str(c)]
        assert len(rm_calls) == 2
