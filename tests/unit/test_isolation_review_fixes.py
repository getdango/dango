"""tests/unit/test_isolation_review_fixes.py

Review follow-ups for the fail-closed isolation states: parent directories of the markers are
trusted only when root-owned and closed, scheduled script runs are labelled unisolated, and the CLI
prints the refusal without a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.platform import run_as
from tests.unit.test_isolation_state import (
    _JOBS,
    _MESSAGE,
    Markers,
    _fake_popen,
    _project,
    cloud,
    markers,
)

pytestmark = pytest.mark.unit

__all__ = ["cloud", "markers"]


class TestParentDirectories:
    def test_group_writable_runner_directory_is_not_trusted(
        self, markers: Markers, cloud: None
    ) -> None:
        markers.write(markers.active)
        assert run_as.isolation_state() == "on"
        markers.base.chmod(0o775)
        assert run_as.isolation_state() == "missing"

    def test_other_writable_srv_directory_is_not_trusted(
        self, markers: Markers, cloud: None
    ) -> None:
        markers.write(markers.disabled)
        assert run_as.isolation_state() == "disabled"
        parent = markers.base.parent
        parent.chmod(0o757)
        try:
            assert run_as.isolation_state() == "missing"
        finally:
            parent.chmod(0o755)

    def test_symlinked_runner_directory_is_not_trusted(
        self, markers: Markers, cloud: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers.write(markers.active)
        link = markers.base.parent / "link"
        link.symlink_to(markers.base)
        monkeypatch.setattr(run_as, "MARKER", str(link / "active"))
        assert run_as.isolation_state() == "missing"


class TestScheduledRunLabel:
    def _run(self, project: Path) -> None:
        from dango.platform.scheduling.jobs import run_scheduled_script

        with (
            patch("subprocess.Popen", return_value=_fake_popen()),
            patch("dango.utils.activity_log.log_activity"),
            patch(f"{_JOBS}._broadcast"),
            patch(f"{_JOBS}._notify"),
            patch(f"{_JOBS}._try_record_start", return_value=1),
            patch(f"{_JOBS}._try_finish_record"),
            patch(f"{_JOBS}._log_execution_event"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
            patch(f"{_JOBS}._scheduler_service", None),
        ):
            run_scheduled_script("nightly", script_path="hello.py", project_root=str(project))

    def _meta(self, project: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        runs = project / ".dango" / "logs" / "scripts" / "runs"
        meta = json.loads(next(runs.glob("*/meta.json")).read_text())
        history = (project / ".dango" / "logs" / "scripts" / "hello.py.jsonl").read_text()
        return meta, json.loads(history.splitlines()[-1])

    def test_disabled_state_marks_meta_and_history(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        markers.write(markers.disabled)
        project = _project(tmp_path)
        self._run(project)
        meta, history = self._meta(project)
        assert meta["unisolated"] is True
        assert history["unisolated"] is True

    def test_warehouse_write_in_state_on_is_marked(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        markers.write(markers.active)
        project = _project(tmp_path, "scripts:\n  - path: hello.py\n    warehouse: write\n")
        self._run(project)
        meta, history = self._meta(project)
        assert meta["unisolated"] is True and history["unisolated"] is True

    def test_local_run_is_not_marked(
        self, markers: Markers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        project = _project(tmp_path)
        self._run(project)
        meta, history = self._meta(project)
        assert "unisolated" not in meta and "unisolated" not in history


class TestFriendlyRefusal:
    def _assert_friendly(self, result: Any) -> None:
        assert result.exit_code == 1, result.output
        assert _MESSAGE in " ".join(result.output.split())
        assert "Traceback" not in result.output
        assert not isinstance(result.exception, run_as.IsolationRequiredError)

    def test_snapshot_db(self, tmp_path: Path) -> None:
        from dango.cli.commands.snapshot import snapshot_db

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch(
                "dango.notebooks.snapshot.create_snapshot",
                side_effect=run_as.IsolationRequiredError(),
            ),
        ):
            result = CliRunner().invoke(snapshot_db, [])
        self._assert_friendly(result)

    @pytest.mark.parametrize("where", ["dango.notebooks.manager.start_marimo", "snapshot"])
    def test_notebook_open(self, tmp_path: Path, where: str) -> None:
        from dango.cli.commands.notebook import notebook_open

        (tmp_path / "notebooks").mkdir()
        (tmp_path / "notebooks" / "nb.py").write_text("x = 1\n")
        error = run_as.IsolationRequiredError()
        snap: dict[str, Any] = (
            {"side_effect": error} if where == "snapshot" else {"return_value": tmp_path / "s"}
        )
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.notebooks.locking.acquire_lock", return_value=True),
            patch("dango.notebooks.locking.refresh_lock"),
            patch("dango.notebooks.locking.release_lock") as release,
            patch("dango.notebooks.manager.ensure_marimo_protected", return_value=False),
            patch("dango.notebooks.manager.get_marimo_status", return_value={"running": False}),
            patch("dango.notebooks.snapshot.create_snapshot", **snap),
            patch("dango.notebooks.manager.start_marimo", side_effect=error),
        ):
            result = CliRunner().invoke(notebook_open, ["nb"])
        self._assert_friendly(result)
        release.assert_called_once()
