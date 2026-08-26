"""tests/unit/test_script_exception_history.py

Tests that cancelled, timed-out, and pre-launch-failed script runs write
history entries to the .jsonl file so the Scripts UI can show them.
"""

import json
import uuid
from unittest.mock import patch

import pytest


def _run_id():
    return str(uuid.uuid4())


def _make_kwargs(tmp_path):
    return {"project_root": str(tmp_path)}


def _history_file(tmp_path, script_path):
    safe_name = script_path.replace("/", "__").replace("\\", "__")
    return tmp_path / ".dango" / "logs" / "scripts" / f"{safe_name}.jsonl"


@pytest.mark.unit
class TestScriptExceptionHistory:
    """run_scheduled_script writes history entries for non-happy-path outcomes."""

    def _setup_valid_script(self, tmp_path, content="print('hello')"):
        """Create a valid scripts/ directory and script file."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "my_script.py"
        script.write_text(content)
        return "my_script.py"

    @patch("dango.platform.scheduling.jobs._scheduler_service", None)
    def test_cancelled_run_writes_history(self, tmp_path):
        """JobCancelledError path writes a 'cancelled' history entry.

        The exception is injected via ``Popen.side_effect`` rather than the
        actual pre-launch ``is_cancelled()`` check (jobs.py) for test
        convenience — either way it's uncaught until the ``except
        JobCancelledError`` handler, so this still exercises the real handler.
        The message mirrors production's raise site so the assertion checks
        that the handler preserves the real exception detail, not a
        hardcoded placeholder.
        """
        from dango.exceptions import JobCancelledError
        from dango.platform.scheduling.jobs import run_scheduled_script

        script_path = self._setup_valid_script(tmp_path)
        cancel_message = "Script cancelled before launch for test_schedule"

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.platform.scheduling.jobs._broadcast"),
            patch("dango.platform.scheduling.jobs._notify"),
            patch("dango.platform.scheduling.jobs._try_record_start", return_value=1),
            patch("dango.platform.scheduling.jobs._try_finish_record"),
            patch("dango.platform.scheduling.jobs._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
        ):
            mock_popen.side_effect = JobCancelledError(cancel_message)

            run_scheduled_script(
                "test_schedule",
                script_path=script_path,
                **_make_kwargs(tmp_path),
            )

        hist = _history_file(tmp_path, script_path)
        assert hist.exists(), "No .jsonl written for cancelled run"
        entry = json.loads(hist.read_text().strip())
        assert entry["status"] == "cancelled"
        assert entry["script_name"] == script_path
        assert entry["error"] == cancel_message
        assert entry["exit_code"] == -1
        assert "run_id" in entry
        assert "started_at" in entry

    @patch("dango.platform.scheduling.jobs._scheduler_service", None)
    def test_pre_launch_exception_writes_history(self, tmp_path):
        """Exception before subprocess launches writes a 'failed' history entry."""
        from dango.platform.scheduling.jobs import run_scheduled_script

        script_path = self._setup_valid_script(tmp_path)

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.platform.scheduling.jobs._broadcast"),
            patch("dango.platform.scheduling.jobs._notify"),
            patch("dango.platform.scheduling.jobs._try_record_start", return_value=1),
            patch("dango.platform.scheduling.jobs._try_finish_record"),
            patch("dango.platform.scheduling.jobs._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
        ):
            mock_popen.side_effect = OSError("Permission denied")

            run_scheduled_script(
                "test_schedule",
                script_path=script_path,
                **_make_kwargs(tmp_path),
            )

        hist = _history_file(tmp_path, script_path)
        assert hist.exists(), "No .jsonl written for pre-launch failed run"
        entry = json.loads(hist.read_text().strip())
        assert entry["status"] == "failed"
        assert "Permission denied" in entry["error"]
        assert entry["exit_code"] == -1
        assert "run_id" in entry

    @patch("dango.platform.scheduling.jobs._scheduler_service", None)
    def test_job_timeout_writes_history(self, tmp_path):
        """JobTimeoutError handler writes a 'timeout' history entry.

        NOTE: run_scheduled_script is not currently wrapped in
        run_with_resilience() (unlike run_scheduled_sync/run_scheduled_dbt),
        so nothing raises JobTimeoutError for SCRIPT schedules in production
        today — this test validates the handler's own logic in isolation
        (defensive parity with the sync/dbt handlers), not a reachable
        production trigger. See the comment above the except block in
        jobs.py.
        """
        from dango.exceptions import JobTimeoutError
        from dango.platform.scheduling.jobs import run_scheduled_script

        script_path = self._setup_valid_script(tmp_path)

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.platform.scheduling.jobs._broadcast"),
            patch("dango.platform.scheduling.jobs._notify"),
            patch("dango.platform.scheduling.jobs._try_record_start", return_value=1),
            patch("dango.platform.scheduling.jobs._try_finish_record"),
            patch("dango.platform.scheduling.jobs._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
        ):
            mock_popen.side_effect = JobTimeoutError("timed out")

            run_scheduled_script(
                "test_schedule",
                script_path=script_path,
                **_make_kwargs(tmp_path),
            )

        hist = _history_file(tmp_path, script_path)
        assert hist.exists(), "No .jsonl written for timed-out run"
        entry = json.loads(hist.read_text().strip())
        assert entry["status"] == "timeout"
        assert "timed out" in entry["error"].lower()
        assert entry["exit_code"] == -1
        assert "run_id" in entry
