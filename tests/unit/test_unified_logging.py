"""tests/unit/test_unified_logging.py

Tests for unified activity logging: category field, stale model cascade,
crash handling helpers, category filtering, and SIGTERM handler.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# activity_log.py — category field
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestActivityLogCategory:
    def test_default_category_is_core(self, tmp_path: Path):
        from dango.utils.activity_log import log_activity

        log_activity(tmp_path, "info", "test", "hello")

        log_file = tmp_path / ".dango" / "logs" / "activity.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["category"] == "core"

    def test_explicit_auxiliary_category(self, tmp_path: Path):
        from dango.utils.activity_log import log_activity

        log_activity(tmp_path, "info", "query", "SELECT 1", category="auxiliary")

        log_file = tmp_path / ".dango" / "logs" / "activity.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["category"] == "auxiliary"

    def test_old_entries_without_category_treated_as_core(self, tmp_path: Path):
        """Old entries lacking category field should be treated as core by consumers."""
        log_dir = tmp_path / ".dango" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "activity.jsonl"

        # Write an old-style entry without category
        old_entry = {
            "timestamp": "2026-01-01T00:00:00",
            "level": "info",
            "source": "test",
            "message": "old",
        }
        log_file.write_text(json.dumps(old_entry) + "\n")

        from dango.web.helpers import load_all_logs

        with patch("dango.web.helpers.get_logs_file", return_value=log_file):
            # category="core" should include old entries (missing = core)
            logs = load_all_logs(category="core")
            assert len(logs) == 1

            # category="auxiliary" should exclude old entries
            logs = load_all_logs(category="auxiliary")
            assert len(logs) == 0


# ---------------------------------------------------------------------------
# dbt_status.py — mark_source_models_stale
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMarkSourceModelsStale:
    def _setup_manifest(self, tmp_path: Path, models: dict) -> None:
        """Write a minimal manifest.json with the given models."""
        manifest = {"nodes": models}
        manifest_path = tmp_path / "dbt" / "target" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest))

    def _setup_status(self, tmp_path: Path, statuses: dict) -> None:
        """Write a dbt_model_status.json."""
        status_path = tmp_path / ".dango" / "dbt_model_status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(statuses))

    def _read_status(self, tmp_path: Path) -> dict:
        status_path = tmp_path / ".dango" / "dbt_model_status.json"
        return json.loads(status_path.read_text())

    def test_marks_dependent_model_stale(self, tmp_path: Path):
        from dango.utils.dbt_status import mark_source_models_stale

        self._setup_manifest(
            tmp_path,
            {
                "model.project.my_model": {
                    "resource_type": "model",
                    "depends_on": {"nodes": ["source.project.my_source.table1"]},
                }
            },
        )
        self._setup_status(
            tmp_path, {"model.project.my_model": {"status": "success", "last_run": "2026-01-01"}}
        )

        mark_source_models_stale(tmp_path, ["my_source"])

        result = self._read_status(tmp_path)
        assert result["model.project.my_model"]["status"] == "stale"
        assert result["model.project.my_model"]["last_run"] == "2026-01-01"

    def test_does_not_overwrite_error_with_stale(self, tmp_path: Path):
        from dango.utils.dbt_status import mark_source_models_stale

        self._setup_manifest(
            tmp_path,
            {
                "model.project.my_model": {
                    "resource_type": "model",
                    "depends_on": {"nodes": ["source.project.my_source.table1"]},
                }
            },
        )
        self._setup_status(
            tmp_path, {"model.project.my_model": {"status": "error", "last_run": "2026-01-01"}}
        )

        mark_source_models_stale(tmp_path, ["my_source"])

        result = self._read_status(tmp_path)
        assert result["model.project.my_model"]["status"] == "error"

    def test_skips_unrelated_sources(self, tmp_path: Path):
        from dango.utils.dbt_status import mark_source_models_stale

        self._setup_manifest(
            tmp_path,
            {
                "model.project.my_model": {
                    "resource_type": "model",
                    "depends_on": {"nodes": ["source.project.other_source.table1"]},
                }
            },
        )
        self._setup_status(
            tmp_path, {"model.project.my_model": {"status": "success", "last_run": "2026-01-01"}}
        )

        mark_source_models_stale(tmp_path, ["my_source"])

        result = self._read_status(tmp_path)
        assert result["model.project.my_model"]["status"] == "success"

    def test_no_manifest_silently_returns(self, tmp_path: Path):
        from dango.utils.dbt_status import mark_source_models_stale

        # No manifest file — should not raise
        mark_source_models_stale(tmp_path, ["my_source"])

    def test_empty_failed_sources_returns_early(self, tmp_path: Path):
        from dango.utils.dbt_status import mark_source_models_stale

        mark_source_models_stale(tmp_path, [])


# ---------------------------------------------------------------------------
# sync_process.py — crash handling helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadLogTail:
    def test_reads_last_n_lines(self, tmp_path: Path):
        from dango.platform.sync_process import _read_log_tail

        log_path = tmp_path / "test.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(100)))

        result = _read_log_tail(log_path, max_lines=5)
        lines = result.split("\n")
        assert len(lines) == 5
        assert lines[-1] == "line 99"

    def test_missing_file_returns_empty(self, tmp_path: Path):
        from dango.platform.sync_process import _read_log_tail

        result = _read_log_tail(tmp_path / "nonexistent.log")
        assert result == ""


@pytest.mark.unit
class TestCleanupSyncLog:
    def test_deletes_on_success(self, tmp_path: Path):
        from dango.platform.sync_process import _cleanup_sync_log

        log_path = tmp_path / "sync_test.log"
        log_path.write_text("some output")

        _cleanup_sync_log(log_path, keep=False)
        assert not log_path.exists()

    def test_keeps_on_failure(self, tmp_path: Path):
        from dango.platform.sync_process import _cleanup_sync_log

        log_path = tmp_path / "sync_test.log"
        log_path.write_text("some output")

        _cleanup_sync_log(log_path, keep=True)
        assert log_path.exists()


@pytest.mark.unit
class TestHandleCrash:
    def test_writes_sync_history_and_activity_log(self, tmp_path: Path):
        from dango.platform.sync_process import _handle_crash

        log_path = tmp_path / "sync_test.log"
        log_path.write_text("Traceback: something broke")

        _handle_crash(tmp_path, ["src1"], 1, log_path, "exit code 1")

        # Check sync history was written
        history_file = tmp_path / ".dango" / "history" / "src1.json"
        assert history_file.exists()
        history = json.loads(history_file.read_text())
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert "something broke" in history[0]["error_message"]

        # Check activity log was written
        activity_file = tmp_path / ".dango" / "logs" / "activity.jsonl"
        assert activity_file.exists()
        entry = json.loads(activity_file.read_text().strip().split("\n")[-1])
        assert entry["level"] == "error"
        assert "crashed" in entry["message"].lower()

    def test_skips_duplicate_history_entry(self, tmp_path: Path):
        """If subprocess already wrote a recent failure, don't duplicate."""
        # Simulate subprocess having already written a failure
        from datetime import datetime, timezone

        from dango.platform.sync_process import _handle_crash
        from dango.utils.sync_history import save_sync_history_entry

        save_sync_history_entry(
            tmp_path,
            "src1",
            {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "status": "failed",
                "duration_seconds": 5,
                "rows_processed": 0,
                "error_message": "subprocess wrote this",
            },
        )

        _handle_crash(tmp_path, ["src1"], 1, None, "crash handler")

        history_file = tmp_path / ".dango" / "history" / "src1.json"
        history = json.loads(history_file.read_text())
        # Should still be 1 entry (not duplicated)
        assert len(history) == 1
        assert history[0]["error_message"] == "subprocess wrote this"


# ---------------------------------------------------------------------------
# web/helpers.py — load_all_logs category filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadAllLogsCategory:
    def _write_logs(self, log_file: Path, entries: list[dict]) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_filter_core_only(self, tmp_path: Path):
        log_file = tmp_path / "activity.jsonl"
        self._write_logs(
            log_file,
            [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "level": "info",
                    "source": "sync",
                    "message": "a",
                    "category": "core",
                },
                {
                    "timestamp": "2026-01-01T00:00:01",
                    "level": "info",
                    "source": "query",
                    "message": "b",
                    "category": "auxiliary",
                },
            ],
        )

        from dango.web.helpers import load_all_logs

        with patch("dango.web.helpers.get_logs_file", return_value=log_file):
            logs = load_all_logs(category="core")
            assert len(logs) == 1
            assert logs[0]["source"] == "sync"

    def test_filter_none_returns_all(self, tmp_path: Path):
        log_file = tmp_path / "activity.jsonl"
        self._write_logs(
            log_file,
            [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "level": "info",
                    "source": "sync",
                    "message": "a",
                    "category": "core",
                },
                {
                    "timestamp": "2026-01-01T00:00:01",
                    "level": "info",
                    "source": "query",
                    "message": "b",
                    "category": "auxiliary",
                },
            ],
        )

        from dango.web.helpers import load_all_logs

        with patch("dango.web.helpers.get_logs_file", return_value=log_file):
            logs = load_all_logs(category=None)
            assert len(logs) == 2


# ---------------------------------------------------------------------------
# web/app.py — SIGTERM handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSigtermHandler:
    def test_installs_signal_handler(self, tmp_path: Path):
        from dango.web.app import _install_sigterm_logger

        original = signal.getsignal(signal.SIGTERM)
        try:
            _install_sigterm_logger(tmp_path)
            current = signal.getsignal(signal.SIGTERM)
            assert current != original
            assert callable(current)
        finally:
            signal.signal(signal.SIGTERM, original)

    def test_handler_writes_activity_log_and_chains(self, tmp_path: Path):
        from dango.web.app import _install_sigterm_logger

        original = signal.getsignal(signal.SIGTERM)
        chain_called = []

        def fake_original(signum, frame):
            chain_called.append((signum, frame))

        try:
            # Set a real function as the "original" handler so we can verify chaining
            signal.signal(signal.SIGTERM, fake_original)
            _install_sigterm_logger(tmp_path)
            handler = signal.getsignal(signal.SIGTERM)

            # Call the installed handler directly
            handler(signal.SIGTERM, None)

            # Verify activity log was written
            activity_file = tmp_path / ".dango" / "logs" / "activity.jsonl"
            assert activity_file.exists()
            entry = json.loads(activity_file.read_text().strip())
            assert entry["level"] == "warning"
            assert "SIGTERM" in entry["message"]

            # Verify original handler was chained
            assert len(chain_called) == 1
            assert chain_called[0][0] == signal.SIGTERM
        finally:
            signal.signal(signal.SIGTERM, original)
