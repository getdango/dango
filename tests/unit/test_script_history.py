"""tests/unit/test_script_history.py

Unit tests for scheduled script history writing in jobs.py.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest


@pytest.mark.unit
class TestWriteScriptLogsHistory:
    """_write_script_logs must append to .jsonl for scripts UI."""

    def test_jsonl_written_on_success(self, tmp_path):
        """Successful run writes to .jsonl history file."""
        from dango.platform.scheduling.jobs import _write_script_logs

        run_id = str(uuid.uuid4())
        started_at = datetime.now(tz=timezone.utc)
        _write_script_logs(
            project_root=tmp_path,
            script_path="my_script.py",
            schedule_name="test_schedule",
            stdout="hello",
            stderr="",
            exit_code=0,
            duration_seconds=1.5,
            status="success",
            run_id=run_id,
            started_at=started_at,
        )

        history_file = tmp_path / ".dango" / "logs" / "scripts" / "my_script.py.jsonl"
        assert history_file.exists()
        entry = json.loads(history_file.read_text().strip())
        assert entry["status"] == "success"
        assert entry["script_name"] == "my_script.py"
        assert entry["exit_code"] == 0
        assert entry["run_id"] == run_id
        assert entry["started_at"] == started_at.isoformat()
        assert entry["error"] is None
        assert "finished_at" in entry

    def test_jsonl_written_on_failure(self, tmp_path):
        """Failed run writes to .jsonl history file with failure status."""
        from dango.platform.scheduling.jobs import _write_script_logs

        run_id = str(uuid.uuid4())
        started_at = datetime.now(tz=timezone.utc)
        _write_script_logs(
            project_root=tmp_path,
            script_path="my_script.py",
            schedule_name="test_schedule",
            stdout="",
            stderr="error output",
            exit_code=1,
            duration_seconds=0.5,
            status="failed",
            run_id=run_id,
            started_at=started_at,
            error="Exit code: 1",
        )

        history_file = tmp_path / ".dango" / "logs" / "scripts" / "my_script.py.jsonl"
        assert history_file.exists()
        entry = json.loads(history_file.read_text().strip())
        assert entry["status"] == "failed"
        assert entry["exit_code"] == 1
        assert entry["error"] == "Exit code: 1"

    def test_jsonl_appends_multiple_runs(self, tmp_path):
        """Multiple runs append to same .jsonl file."""
        from dango.platform.scheduling.jobs import _write_script_logs

        for i in range(3):
            _write_script_logs(
                project_root=tmp_path,
                script_path="my_script.py",
                schedule_name="test_schedule",
                stdout=f"run {i}",
                stderr="",
                exit_code=0,
                duration_seconds=float(i),
                status="success",
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(tz=timezone.utc),
            )

        history_file = tmp_path / ".dango" / "logs" / "scripts" / "my_script.py.jsonl"
        lines = [line for line in history_file.read_text().strip().split("\n") if line]
        assert len(lines) == 3

    def test_jsonl_path_matches_scripts_helper(self, tmp_path):
        """History file path matches what scripts_helpers._get_history_file returns."""
        from dango.platform.scheduling.jobs import _write_script_logs
        from dango.web.routes.scripts_helpers import _get_history_file

        _write_script_logs(
            project_root=tmp_path,
            script_path="my_script.py",
            schedule_name="s",
            stdout="",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            status="success",
            run_id=str(uuid.uuid4()),
            started_at=datetime.now(tz=timezone.utc),
        )

        expected = _get_history_file(tmp_path, "my_script.py")
        assert expected.exists()

    def test_log_dir_matches_web_log_viewer_path(self, tmp_path):
        """stdout/stderr/meta land under runs/{run_id}, matching _get_log_dir()."""
        from dango.platform.scheduling.jobs import _write_script_logs
        from dango.web.routes.scripts_helpers import _get_log_dir

        run_id = str(uuid.uuid4())
        _write_script_logs(
            project_root=tmp_path,
            script_path="my_script.py",
            schedule_name="s",
            stdout="hello",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            status="success",
            run_id=run_id,
            started_at=datetime.now(tz=timezone.utc),
        )

        expected_log_dir = _get_log_dir(tmp_path) / run_id
        assert (expected_log_dir / "stdout.txt").read_text() == "hello"
        assert (expected_log_dir / "meta.json").exists()
