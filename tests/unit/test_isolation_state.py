"""tests/unit/test_isolation_state.py

Decision D2: on a cloud server whose isolation marker is missing, user code
(scripts, scheduled scripts, Marimo, notebook snapshots) refuses to run instead of
silently running unisolated.  A second root-owned marker is an audited admin opt-out
(``disabled``); it restores the legacy behaviour and marks the run unisolated.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import DangoError
from dango.platform import run_as, script_launch

_MESSAGE = (
    "Script and notebook isolation is not set up on this server. "
    "Run `dango remote upgrade` to enable it "
    "(an admin can opt out with `dango remote isolation disable`)."
)
_CODE = "DANGO-ISO001"
_API = "dango.web.routes.scripts_api"
_HELPERS = "dango.web.routes.scripts_helpers"
_JOBS = "dango.platform.scheduling.jobs"
_NB = "dango.web.routes.notebooks"


class Markers:
    """Real marker files in a tmp dir, with the root-owner check pointed at this user."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.base = base
        self.active = base / "active"
        self.disabled = base / "disabled"
        monkeypatch.setattr(run_as, "MARKER", str(self.active))
        monkeypatch.setattr(run_as, "DISABLED_MARKER", str(self.disabled))
        monkeypatch.setattr(run_as, "_ROOT_UID", os.getuid())
        self.monkeypatch = monkeypatch

    def write(self, which: Path, mode: int = 0o644) -> None:
        which.write_text("")
        which.chmod(mode)


@pytest.fixture
def markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Markers:
    base = tmp_path / "runner"
    base.mkdir()
    return Markers(base, monkeypatch)


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANGO_CLOUD_MODE", "true")


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


def _admin() -> User:
    return User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)


def _project(tmp_path: Path, scripts_yml: str | None = None) -> Path:
    project = tmp_path / "proj"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "hello.py").write_text("print('hi')\n")
    (project / ".dango").mkdir()
    (project / "data").mkdir()
    (project / "data" / "warehouse.duckdb").write_bytes(b"duck")
    if scripts_yml:
        (project / ".dango" / "scripts.yml").write_text(scripts_yml)
    return project


def _fake_popen() -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 4242
    proc.returncode = 0
    proc.poll.return_value = None
    proc.communicate.return_value = ("out", "")
    return proc


class TestStateMatrix:
    def test_local_when_not_on_cloud_and_no_marker(self, markers: Markers, local: None) -> None:
        assert run_as.isolation_state() == "local"
        assert run_as.uses_isolation() is False

    def test_valid_marker_means_on_even_without_the_cloud_env_var(
        self, markers: Markers, local: None
    ) -> None:
        markers.write(markers.active)
        assert run_as.isolation_state() == "on"
        assert run_as.uses_isolation() is True

    def test_valid_disabled_marker_means_disabled_without_the_cloud_env_var(
        self, markers: Markers, local: None
    ) -> None:
        markers.write(markers.disabled)
        assert run_as.isolation_state() == "disabled"
        markers.write(markers.active)
        assert run_as.isolation_state() == "disabled"

    def test_untrusted_marker_without_the_cloud_env_var_is_local(
        self, markers: Markers, local: None
    ) -> None:
        markers.write(markers.active, 0o666)
        assert run_as.isolation_state() == "local"

    def test_missing_on_cloud_without_markers(self, markers: Markers, cloud: None) -> None:
        assert run_as.isolation_state() == "missing"
        assert run_as.uses_isolation() is False

    def test_on_with_valid_marker(self, markers: Markers, cloud: None) -> None:
        markers.write(markers.active)
        assert run_as.isolation_state() == "on"
        assert run_as.uses_isolation() is True

    def test_disabled_with_valid_disabled_marker(self, markers: Markers, cloud: None) -> None:
        markers.write(markers.disabled)
        assert run_as.isolation_state() == "disabled"
        assert run_as.uses_isolation() is False

    def test_both_markers_means_disabled(self, markers: Markers, cloud: None) -> None:
        markers.write(markers.active)
        markers.write(markers.disabled)
        assert run_as.isolation_state() == "disabled"

    def test_symlink_marker_is_not_trusted(self, markers: Markers, cloud: None) -> None:
        real = markers.base / "real"
        markers.write(real)
        markers.active.symlink_to(real)
        assert run_as.isolation_state() == "missing"

    def test_symlink_disabled_marker_is_not_trusted(self, markers: Markers, cloud: None) -> None:
        real = markers.base / "real"
        markers.write(real)
        markers.disabled.symlink_to(real)
        markers.write(markers.active)
        assert run_as.isolation_state() == "on"

    @pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
    def test_group_or_other_writable_marker_is_not_trusted(
        self, markers: Markers, cloud: None, mode: int
    ) -> None:
        markers.write(markers.active, mode)
        assert run_as.isolation_state() == "missing"
        markers.write(markers.disabled, mode)
        assert run_as.isolation_state() == "missing"

    def test_wrong_owner_marker_is_not_trusted(
        self, markers: Markers, cloud: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers.write(markers.active)
        markers.write(markers.disabled)
        monkeypatch.setattr(run_as, "_ROOT_UID", os.getuid() + 1)
        assert run_as.isolation_state() == "missing"

    def test_directory_marker_is_not_trusted(self, markers: Markers, cloud: None) -> None:
        markers.disabled.mkdir()
        assert run_as.isolation_state() == "missing"

    def test_disabled_marker_default_path(self) -> None:
        assert run_as.DISABLED_MARKER == "/srv/dango/runner/disabled"


def _assert_refusal(exc: BaseException) -> None:
    assert isinstance(exc, DangoError)
    assert exc.error_code == _CODE
    assert exc.user_message == _MESSAGE
    assert str(exc) == _MESSAGE


class TestRefusalWhenMissing:
    def test_require_ready_message_and_code(self, markers: Markers, cloud: None) -> None:
        with pytest.raises(DangoError) as info:
            run_as.require_isolation_ready()
        _assert_refusal(info.value)

    def test_require_ready_returns_state(self, markers: Markers, cloud: None, local: None) -> None:
        assert run_as.require_isolation_ready() == "local"

    def test_build_launch_refuses(self, markers: Markers, cloud: None, tmp_path: Path) -> None:
        with pytest.raises(DangoError) as info:
            run_as.build_launch("nb", [], project_root=tmp_path, legacy_argv=["x"])
        _assert_refusal(info.value)

    def test_script_launch_refuses_and_starts_nothing(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        project = _project(tmp_path)
        with patch("subprocess.Popen") as popen, pytest.raises(DangoError) as info:
            script_launch.start_script(project, "hello.py", project / "scripts" / "hello.py")
        _assert_refusal(info.value)
        popen.assert_not_called()

    def test_warehouse_write_script_refuses_too(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        project = _project(
            tmp_path, "scripts:\n  - path: hello.py\n    env: [X]\n    warehouse: write\n"
        )
        with patch("subprocess.Popen") as popen, pytest.raises(DangoError) as info:
            script_launch.start_script(project, "hello.py", project / "scripts" / "hello.py")
        _assert_refusal(info.value)
        popen.assert_not_called()

    def test_web_run_refuses_before_creating_a_run(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        import dango.web.routes.scripts_helpers as helpers
        from dango.web.routes.scripts_api import run_script

        project = _project(tmp_path)
        helpers._running_processes.clear()

        async def scenario() -> None:
            with (
                patch(f"{_API}.get_project_root", return_value=project),
                patch(f"{_API}.append_log_entry"),
                patch(f"{_API}.ws_manager") as ws,
                patch(f"{_HELPERS}.log_auth_event"),
                patch("subprocess.Popen") as popen,
            ):
                ws.broadcast = AsyncMock()
                with pytest.raises(DangoError) as info:
                    await run_script(
                        name="hello.py", request=SimpleNamespace(client=None), user=_admin()
                    )
                _assert_refusal(info.value)
                popen.assert_not_called()

        asyncio.run(scenario())
        assert not helpers._running_processes
        assert not list(helpers._get_log_dir(project).glob("*/meta.json"))

    def test_scheduled_run_fails_with_the_message(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.platform.scheduling.jobs import run_scheduled_script

        project = _project(tmp_path)
        with (
            patch("subprocess.Popen") as popen,
            patch("dango.utils.activity_log.log_activity") as activity,
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
        popen.assert_not_called()
        messages = [c.args[3] for c in activity.call_args_list if len(c.args) > 3]
        assert any(_MESSAGE in m for m in messages)

    def test_start_marimo_refuses_and_starts_nothing(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.notebooks import manager

        with patch("subprocess.Popen") as popen, pytest.raises(DangoError) as info:
            manager.start_marimo(tmp_path, port=7805)
        _assert_refusal(info.value)
        popen.assert_not_called()
        assert not (tmp_path / "notebooks").exists()

    def test_snapshot_refuses_and_copies_nothing(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.notebooks.snapshot import create_snapshot

        project = _project(tmp_path)
        with pytest.raises(DangoError) as info:
            create_snapshot(project, "u")
        _assert_refusal(info.value)
        assert not (project / ".dango" / "snapshots").exists()


def _nb_client(project: Path) -> TestClient:
    from dango.web.routes.notebooks import router

    app = FastAPI()

    @app.exception_handler(DangoError)
    async def _handler(request: Request, exc: DangoError) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"error_code": exc.error_code, "message": exc.user_message}
        )

    @app.middleware("http")
    async def _user(request: Any, call_next: Any) -> Any:
        request.state.user = _admin()
        request.state.auth_method = "session"
        return await call_next(request)

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


class TestNotebookRoutes:
    def test_lock_route_refuses_before_taking_the_lock(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.utils.dango_db import _schema_initialized, connect

        project = _project(tmp_path)
        (project / "notebooks").mkdir()
        (project / "notebooks" / "nb.py").write_text("# nb\n")
        _schema_initialized.clear()
        with connect(project):
            pass
        with (
            patch(f"{_NB}.get_project_root", return_value=project),
            patch(f"{_NB}.start_marimo") as start,
            patch(f"{_NB}.create_snapshot") as snap,
        ):
            resp = _nb_client(project).post("/api/notebooks/nb/lock", json={})
        assert resp.status_code >= 400
        assert resp.json() == {"error_code": _CODE, "message": _MESSAGE}
        start.assert_not_called()
        snap.assert_not_called()
        with connect(project) as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM notebook_locks").fetchone()["c"] == 0

    def test_restart_route_only_stops_so_it_still_works(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.utils.dango_db import _schema_initialized, connect

        project = _project(tmp_path)
        _schema_initialized.clear()
        with connect(project):
            pass
        with (
            patch(f"{_NB}.get_project_root", return_value=project),
            patch(f"{_NB}.stop_marimo", return_value=True) as stop,
            patch(f"{_NB}.log_auth_event"),
        ):
            resp = _nb_client(project).post("/api/notebooks/marimo/restart")
        assert resp.status_code == 200
        stop.assert_called_once()


class TestDisabled:
    def test_script_runs_legacy_and_is_marked_unisolated(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        markers.write(markers.disabled)
        project = _project(tmp_path)
        with patch("subprocess.Popen", return_value=_fake_popen()) as popen:
            started = script_launch.start_script(
                project, "hello.py", project / "scripts" / "hello.py", run_id="r1"
            )
        assert popen.call_args.args[0] == [sys.executable, str(project / "scripts" / "hello.py")]
        assert "start_new_session" not in popen.call_args.kwargs
        assert started.isolated is False
        assert started.unisolated is True

    def test_web_run_marks_meta_and_audit(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        import dango.web.routes.scripts_helpers as helpers
        from dango.web.routes.scripts_api import run_script

        markers.write(markers.disabled)
        project = _project(tmp_path)
        helpers._running_processes.clear()

        async def scenario() -> tuple[dict[str, Any], MagicMock]:
            with (
                patch(f"{_API}.get_project_root", return_value=project),
                patch(f"{_API}._get_script_timeout", return_value=10),
                patch(f"{_API}.append_log_entry"),
                patch(f"{_API}.ws_manager") as ws,
                patch(f"{_HELPERS}.log_auth_event") as audit,
                patch("subprocess.Popen", return_value=_fake_popen()),
            ):
                ws.broadcast = AsyncMock()
                resp = await run_script(
                    name="hello.py", request=SimpleNamespace(client=None), user=_admin()
                )
                await asyncio.sleep(0.2)
                return json.loads(bytes(resp.body)), audit

        body, audit = asyncio.run(scenario())
        assert body["unisolated"] is True
        details = next(c for c in audit.call_args_list).kwargs["details"]
        assert details["unisolated"] is True

    def test_build_launch_is_legacy(self, markers: Markers, cloud: None, tmp_path: Path) -> None:
        markers.write(markers.disabled)
        launch = run_as.build_launch("nb", ["a"], project_root=tmp_path, legacy_argv=["legacy"])
        assert launch.argv == ["legacy"]
        assert launch.isolated is False

    def test_marimo_starts_legacy_with_one_warning(
        self,
        markers: Markers,
        cloud: None,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from dango.notebooks import manager

        markers.write(markers.disabled)

        class _Proc:
            pid = os.getpid()
            returncode: int | None = None

            def poll(self) -> int | None:
                return None

        caplog.set_level("WARNING", logger="dango.notebooks.manager")
        with (
            patch(f"{manager.__name__}.subprocess.Popen", return_value=_Proc()) as popen,
            patch(f"{manager.__name__}._is_marimo_responding", return_value=True),
            patch(f"{manager.__name__}.time.sleep"),
        ):
            manager.start_marimo(tmp_path, port=7805)
        argv = popen.call_args[0][0]
        assert argv[0] != run_as.SUDO
        assert "dango.runtime.isolated_run" not in argv
        warnings = [r for r in caplog.records if "isolation" in r.getMessage().lower()]
        assert len(warnings) == 1

    def test_snapshot_uses_legacy_directory(
        self, markers: Markers, cloud: None, tmp_path: Path
    ) -> None:
        from dango.notebooks.snapshot import create_snapshot

        markers.write(markers.disabled)
        project = _project(tmp_path)
        path = create_snapshot(project, "u")
        assert path.parent == project / ".dango" / "snapshots"
