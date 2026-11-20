"""tests/unit/test_docker_absent_commands.py

CLI behaviour with Docker missing or stopped: no traceback, one quiet line where Docker is optional,
an OS-correct actionable failure where it is required.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli
from dango.utils import docker_hints
from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NOTE = "Metabase not refreshed: Docker is not available"
_LEAKS = ("Traceback", "FileNotFoundError", "Errno 2", "No such file", "'docker-compose'")


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    monkeypatch.setitem(docker_hints._printed, "metabase", False)


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = working_copy(fresh_project, tmp_path)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A PATH without a docker binary: the real subprocess raises FileNotFoundError."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _invoke(argv: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli, argv)
    return result.exit_code, _ANSI.sub("", result.output)


def _assert_clean(output: str) -> None:
    for leak in _LEAKS:
        assert leak not in output, f"{leak!r} leaked:\n{output}"


class _SelectiveRun:
    """subprocess.run that fails like a missing docker binary and fakes the dbt CLI."""

    def __init__(self) -> None:
        self.real = subprocess.run

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        head = Path(str(argv[0])).name if argv else ""
        if head in {"docker", "docker-compose"}:
            raise FileNotFoundError(2, "No such file or directory", head)
        if head == "dbt":
            return subprocess.CompletedProcess(argv, 0)
        return self.real(argv, *args, **kwargs)


# --- sync / run: Metabase steps skip quietly ----------------------------------------------


def _sync_patches() -> list[Any]:
    return [
        patch("dango.transformation.run_dbt_models", return_value=(True, "")),
        patch("dango.transformation.generate_dbt_docs", return_value=(True, "")),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value={}),
    ]


def test_sync_without_docker_prints_one_quiet_line(
    project: Path, no_docker: None, caplog: pytest.LogCaptureFixture
) -> None:
    patches = _sync_patches()
    for p in patches:
        p.start()
    try:
        with caplog.at_level(logging.DEBUG):
            code, output = _invoke(["sync", "--yes"])
    finally:
        for p in patches:
            p.stop()
    assert code == 0, output
    _assert_clean(output)
    assert output.count(_NOTE) == 1, output
    assert "Metabase not running" not in output
    assert "Updating Metabase schema" not in output
    assert not [r for r in caplog.records if r.exc_info], "a traceback was logged"


def test_run_without_docker_prints_one_quiet_line(
    project: Path, no_docker: None, caplog: pytest.LogCaptureFixture
) -> None:
    with patch("subprocess.run", _SelectiveRun()), caplog.at_level(logging.DEBUG):
        code, output = _invoke(["run"])
    assert code == 0, output
    _assert_clean(output)
    assert output.count(_NOTE) == 1, output
    assert not [r for r in caplog.records if r.exc_info]


def test_refresh_reports_docker_unavailable_without_logging_a_traceback(
    project: Path, no_docker: None, caplog: pytest.LogCaptureFixture
) -> None:
    from dango.visualization.metabase import refresh_metabase_connection

    with caplog.at_level(logging.DEBUG):
        ok, err, session = refresh_metabase_connection(project)
    assert (ok, session) == (False, None)
    assert err == "Docker is not available"
    assert not [r for r in caplog.records if r.exc_info]


def test_refresh_treats_a_stopped_daemon_as_docker_unavailable(project: Path) -> None:
    from dango.visualization.metabase import refresh_metabase_connection

    stopped = subprocess.CompletedProcess(["docker", "ps"], 1, stdout="", stderr="Cannot connect")
    with patch("subprocess.run", return_value=stopped):
        ok, err, _ = refresh_metabase_connection(project)
    assert ok is False and err == "Docker is not available"


def test_refresh_with_a_running_docker_that_has_no_metabase_keeps_its_message(
    project: Path,
) -> None:
    from dango.visualization.metabase import refresh_metabase_connection

    listed = subprocess.CompletedProcess(["docker", "ps"], 0, stdout="other\n", stderr="")
    with patch("subprocess.run", return_value=listed):
        ok, err, _ = refresh_metabase_connection(project)
    assert ok is False and err == "Metabase container not running"


# --- commands that must not need Docker ---------------------------------------------------


@pytest.mark.parametrize("argv", [["status"], ["validate"], ["doctor"]])
def test_local_commands_do_not_trip_over_a_missing_docker(
    project: Path, no_docker: None, argv: list[str]
) -> None:
    code, output = _invoke(argv)
    assert code == 0, output
    _assert_clean(output)


def test_web_does_not_need_docker(project: Path, no_docker: None) -> None:
    with patch("uvicorn.run") as serve:
        code, output = _invoke(["web", "--port", "18999"])
    assert code == 0, output
    serve.assert_called_once()
    _assert_clean(output)


# --- commands that need Docker fail with OS-correct guidance ------------------------------


@pytest.mark.parametrize(
    ("platform", "expected", "absent"),
    [
        ("darwin", "Docker Desktop", "systemctl"),
        ("win32", "Docker Desktop", "systemctl"),
        ("linux", "Docker Engine", "Docker Desktop"),
    ],
)
def test_start_without_docker_names_the_right_product(
    project: Path,
    no_docker: None,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: str,
    absent: str,
) -> None:
    monkeypatch.setattr(docker_hints, "_current_platform", lambda: platform)
    with patch("dango.platform.common.startup.ensure_duckdb_driver", MagicMock()):
        code, output = _invoke(["start", "--yes"])
    assert code == 1, output
    _assert_clean(output)
    assert expected in output and absent not in output
    assert "not installed" in output
    assert "Stopping Dango services" not in output
    assert "Docker daemon is not running" not in output


class _StoppedDaemon:
    """A docker binary on PATH whose daemon is down: --version works, everything else fails."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append([str(a) for a in argv])
        if list(argv[:2]) == ["docker", "--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Docker 28", stderr="")
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Cannot connect to the Docker daemon at unix:///x.sock"
        )

    def __enter__(self) -> _StoppedDaemon:
        self._patches = [
            patch("subprocess.run", self),
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("dango.platform.common.startup.ensure_duckdb_driver", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()


def test_start_with_a_stopped_daemon_says_to_start_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_hints, "_current_platform", lambda: "linux")
    with _StoppedDaemon() as daemon:
        code, output = _invoke(["start", "--yes"])
    assert code == 1, output
    assert "systemctl start docker" in output
    assert "Docker Desktop" not in output
    assert "not installed" not in output
    assert "Stopping Dango services" not in output
    assert "Failed to stop services" not in output
    assert "Cannot connect" not in output
    assert not [c for c in daemon.calls if "compose" in c or "docker-compose" in c[0]]


def test_stop_without_docker_stops_local_services_and_exits_0(
    project: Path, no_docker: None
) -> None:
    code, output = _invoke(["stop"])
    assert code == 0, output
    _assert_clean(output)
    assert "Docker is not available" in output
    assert "Install" not in output and "Docker Desktop is not installed" not in output
    assert "Error" not in output


def test_stop_with_a_stopped_daemon_exits_0_like_a_missing_docker(
    project: Path, no_docker: None
) -> None:
    missing_code, _ = _invoke(["stop"])
    with _StoppedDaemon():
        code, output = _invoke(["stop"])
    assert code == missing_code == 0, output
    assert "Docker is not running" in output
    assert "Failed to stop services" not in output and "Cannot connect" not in output
    assert "Error" not in output


def test_stop_marks_the_project_stopped_when_docker_is_missing(
    project: Path, no_docker: None
) -> None:
    network = MagicMock()
    network.return_value.get_project_info.return_value = {"status": "running", "domain": "x"}
    with patch("dango.platform.network.NetworkConfig", network):
        code, output = _invoke(["stop"])
    assert code == 0, output
    network.return_value.update_project_status.assert_called_once()


def test_stop_fails_when_docker_is_reachable_and_stopping_fails(project: Path) -> None:
    from dango.platform.docker import DockerManager

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.object(DockerManager, "is_docker_daemon_running", return_value=True),
        patch.object(DockerManager, "stop_services", return_value=False),
    ):
        code, output = _invoke(["stop"])
    assert code == 1, output


def test_stop_all_without_docker_or_with_a_stopped_daemon_exits_0(
    project: Path, no_docker: None
) -> None:
    code, output = _invoke(["stop", "--all"])
    assert code == 0, output
    _assert_clean(output)
    assert "Docker is not available" in output and "nothing was stopped" in output
    assert "Stopped all Dango containers" not in output
    with _StoppedDaemon():
        code, output = _invoke(["stop", "--all"])
    assert code == 0, output
    assert "Docker is not running" in output
    assert "Could not list Docker containers" not in output
    assert "Stopped all Dango containers" not in output


def test_stop_all_fails_when_docker_is_reachable_and_stopping_fails(project: Path) -> None:
    from dango.platform.docker import DockerManager

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch.object(DockerManager, "is_docker_daemon_running", return_value=True),
        patch.object(DockerManager, "stop_all_dango_containers", return_value=False),
    ):
        code, output = _invoke(["stop", "--all"])
    assert code == 1, output
    assert "Stopped all Dango containers" not in output


def test_stop_services_without_docker_says_it_did_not_check(
    tmp_path: Path, no_docker: None
) -> None:
    from dango.platform.docker import DockerManager

    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    manager = DockerManager(tmp_path)
    with patch("dango.platform.docker.console") as console:
        assert manager.stop_services() is True
    printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "Docker services were not checked" in printed
    assert "no Docker services to stop" not in printed


def test_refresh_with_a_hung_daemon_is_quiet(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import sys

    from dango.visualization.metabase import refresh_metabase_connection

    real_run = subprocess.run

    def hung(argv: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = 0.5
        return real_run([sys.executable, "-c", "import time; time.sleep(30)"], *args, **kwargs)

    with patch("subprocess.run", hung), caplog.at_level(logging.DEBUG):
        ok, err, _ = refresh_metabase_connection(project)
    assert ok is False and err == "Docker is not available"
    assert not [r for r in caplog.records if r.exc_info]


def test_status_and_stop_tolerate_a_home_without_dango_dir(
    project: Path, no_docker: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "missing" / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "missing" / "home"))
    for argv in (["status"], ["stop"]):
        code, output = _invoke(argv)
        assert code == 0, output
        assert "Errno 2" not in output


def test_docker_audit_says_docker_is_not_available(project: Path, no_docker: None) -> None:
    code, output = _invoke(["docker-audit"])
    assert code == 0, output
    assert "Docker is not available" in output
    assert "No dango-* Docker resources found" not in output


def test_docker_audit_with_a_stopped_daemon_says_so(project: Path) -> None:
    with _StoppedDaemon():
        code, output = _invoke(["docker-audit"])
    assert code == 0, output
    assert "Docker is not running" in output
    assert "No dango-* Docker resources found" not in output


def test_cleanup_docker_says_volumes_were_not_checked(project: Path, no_docker: None) -> None:
    code, output = _invoke(["cleanup", "--docker", "--dry-run"])
    assert code == 0, output
    assert "Docker is not available" in output and "not checked" in output


def test_docker_manager_probes_tolerate_any_oserror(tmp_path: Path) -> None:
    from dango.platform.docker import DockerManager

    manager = DockerManager(tmp_path)
    with patch("subprocess.run", side_effect=PermissionError(13, "denied")):
        assert manager.is_docker_available() is False
        assert manager.is_docker_daemon_running() is False
        assert manager.is_compose_available() is False
        assert manager.get_compose_command() == ["docker-compose"]


def test_web_service_probe_is_quiet_without_docker(
    no_docker: None, caplog: pytest.LogCaptureFixture
) -> None:
    from dango.web.helpers import check_service_via_docker

    with caplog.at_level(logging.DEBUG):
        assert check_service_via_docker("metabase") == "unknown"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
