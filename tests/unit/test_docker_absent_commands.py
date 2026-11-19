"""tests/unit/test_docker_absent_commands.py

CLI behaviour with Docker missing or stopped: no traceback, one quiet line where Docker is optional,
an OS-correct actionable failure where it is required.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli
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
    if importlib.util.find_spec("dango.utils.docker_hints") is not None:
        from dango.utils import docker_hints

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
    monkeypatch.setattr(sys, "platform", platform)
    with patch("dango.platform.common.startup.ensure_duckdb_driver", MagicMock()):
        code, output = _invoke(["start", "--yes"])
    assert code == 1, output
    _assert_clean(output)
    assert expected in output and absent not in output
    assert "not installed" in output
    assert "Stopping Dango services" not in output
    assert "Docker daemon is not running" not in output


def test_start_with_a_stopped_daemon_says_to_start_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    ok = subprocess.CompletedProcess(["docker", "--version"], 0, stdout="Docker 28", stderr="")
    down = subprocess.CompletedProcess(["docker", "ps"], 1, stdout="", stderr="Cannot connect")

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if list(argv[:2]) == ["docker", "--version"]:
            return ok
        if list(argv[:2]) == ["docker", "ps"]:
            return down
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    with (
        patch("dango.platform.common.startup.ensure_duckdb_driver", MagicMock()),
        patch("subprocess.run", run),
        patch("shutil.which", return_value="/usr/bin/docker"),
    ):
        code, output = _invoke(["start", "--yes"])
    assert code == 1, output
    assert "systemctl start docker" in output
    assert "Docker Desktop" not in output
    assert "not installed" not in output


def test_stop_without_docker_fails_with_guidance_and_does_not_claim_success(
    project: Path, no_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    code, output = _invoke(["stop"])
    assert code == 1, output
    _assert_clean(output)
    assert "Docker Desktop" in output
    assert "All services stopped" not in output


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


def test_stop_all_without_docker_fails_with_guidance(
    project: Path, no_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    code, output = _invoke(["stop", "--all"])
    assert code == 1, output
    _assert_clean(output)
    assert "Docker Engine" in output
    assert "Stopped all Dango containers" not in output
