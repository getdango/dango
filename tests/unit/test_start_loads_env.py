"""tests/unit/test_start_loads_env.py

`dango start` loads the project `.env` before it starts anything, so the server it spawns and
the scripts that server runs see the same variables `dango serve` already provides.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.platform import start

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_PM = "dango.cli.helpers.process_manager"
_DUMP_CHILD = (
    "import json, os, sys, time\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
    "    json.dump(dict(os.environ), fh)\n"
    "time.sleep(60)\n"
)
_PRINT_SCRIPT = "import os\nprint(repr(os.environ.get('WP35_VAR')))\n"


def _config() -> MagicMock:
    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.metabase_port = 3000
    config.platform.dbt_docs_port = 8081
    config.platform.auto_sync = False
    return config


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WP35_VAR", "WP35_SHELL", "WP35_OTHER", "DANGO_CLOUD_MODE", "DANGO_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)


def _invoke_start(project_root: Path, server: Any, extra: list[Any] | None = None) -> Result:
    """Run the real ``start`` command with Docker, Metabase and the network replaced by fakes.

    *server* replaces ``start_fastapi_server`` (None keeps the real one).
    """
    loader = MagicMock()
    loader.load_config.return_value = _config()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        for target, kwargs in (
            ("dango.cli.utils.check_v01x_project", {}),
            ("dango.cli.utils.require_project_context", {"return_value": project_root}),
            ("dango.config.ConfigLoader", {"return_value": loader}),
            (f"{_STARTUP}.check_duckdb_version_alignment", {}),
            (f"{_STARTUP}.rotate_logs", {}),
            (f"{_STARTUP}.run_pending_migrations", {"return_value": {}}),
            (f"{_STARTUP}.cleanup_stale_dbt_lock", {"return_value": False}),
            (f"{_STARTUP}.ensure_dbt_schemas", {}),
            (f"{_STARTUP}.ensure_duckdb_driver", {}),
            (f"{_STARTUP}.start_docker_services", {}),
            (f"{_STARTUP}.wait_for_metabase_if_needed", {"return_value": True}),
            (f"{_STARTUP}.metabase_startup_work_pending", {"return_value": False}),
            (f"{_STARTUP}.setup_metabase_if_needed", {"return_value": {"success": True}}),
            (f"{_STARTUP}.apply_metabase_duckdb_lock_if_needed", {}),
            (f"{_STARTUP}.import_dashboards", {"return_value": None}),
            (
                "dango.platform.common.metabase_credential_migration."
                "complete_metabase_credential_migration",
                {"return_value": {"status": "secure_rotated"}},
            ),
            ("dango.cli.commands.platform._check_docker_ports", {}),
            ("dango.config.credentials.ensure_sensitive_artifact_gitignores", {}),
            ("dango.platform.watcher_lifecycle.kill_orphan_watchers", {"return_value": 0}),
            ("socket.socket", {}),
            ("subprocess.run", {"return_value": MagicMock(returncode=1, stdout="")}),
            ("requests.get", {"return_value": MagicMock(status_code=200)}),
            ("webbrowser.open", {}),
        ):
            mocked = stack.enter_context(patch(target, **kwargs))
            if target == "socket.socket":
                mocked.return_value.connect_ex.return_value = 1
        if server is not None:
            stack.enter_context(patch(f"{_PM}.start_fastapi_server", side_effect=server))
        for extra_patch in extra or []:
            stack.enter_context(extra_patch)
        return CliRunner().invoke(start, ["--yes"], obj={})


def _start_with_real_child(project_root: Path, out: Path) -> tuple[Result, list[list[str]]]:
    """Run ``start`` with the real ``start_fastapi_server``; the child dumps its environment.

    Only the program the child runs is swapped (uvicorn for a short script): the command line,
    ``env``, ``cwd`` and detach arguments are exactly what ``start`` passes.
    """
    real_popen = subprocess.Popen
    real_sleep = time.sleep
    argvs: list[list[str]] = []
    children: list[subprocess.Popen[Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        argvs.append(list(argv))
        child = real_popen([sys.executable, "-c", _DUMP_CHILD, str(out)], **kwargs)
        children.append(child)
        return child

    def wait_for_dump(_seconds: float) -> None:
        deadline = time.monotonic() + 20
        while not out.exists() and time.monotonic() < deadline:
            real_sleep(0.05)
        real_sleep(0.1)

    try:
        result = _invoke_start(
            project_root,
            None,
            [
                patch(f"{_PM}.subprocess.Popen", side_effect=popen),
                patch(f"{_PM}.time.sleep", side_effect=wait_for_dump),
                patch(f"{_PM}.check_port_in_use", return_value=False),
            ],
        )
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=10)
    return result, argvs


def _child_env(out: Path) -> dict[str, str]:
    return json.loads(out.read_text(encoding="utf-8"))


class TestServerProcessEnvironment:
    def test_dotenv_value_reaches_the_spawned_server(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (project / ".env").write_text("WP35_VAR=from-dotenv\n", encoding="utf-8")
        out = tmp_path / "child_env.json"
        result, argvs = _start_with_real_child(project, out)
        assert result.exit_code == 0, result.output
        env = _child_env(out)
        assert env["WP35_VAR"] == "from-dotenv"
        assert env["DANGO_PROJECT_ROOT"] == str(project)
        assert not any("from-dotenv" in part for argv in argvs for part in argv)

    def test_shell_value_wins_over_dotenv(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WP35_SHELL", "from-shell")
        (project / ".env").write_text(
            "WP35_SHELL=from-dotenv\nWP35_OTHER=from-dotenv\n", encoding="utf-8"
        )
        out = tmp_path / "child_env.json"
        result, _ = _start_with_real_child(project, out)
        assert result.exit_code == 0, result.output
        env = _child_env(out)
        assert env["WP35_SHELL"] == "from-shell"
        assert env["WP35_OTHER"] == "from-dotenv"

    def test_cloud_mode_and_project_root_in_dotenv_are_ignored(
        self, project: Path, tmp_path: Path
    ) -> None:
        (project / ".env").write_text(
            "WP35_VAR=from-dotenv\nDANGO_CLOUD_MODE=true\nDANGO_PROJECT_ROOT=/somewhere/else\n",
            encoding="utf-8",
        )
        out = tmp_path / "child_env.json"
        result, _ = _start_with_real_child(project, out)
        assert result.exit_code == 0, result.output
        env = _child_env(out)
        assert env["WP35_VAR"] == "from-dotenv"  # the file was read ...
        assert "DANGO_CLOUD_MODE" not in env  # ... but these two keys are not taken from it
        assert env["DANGO_PROJECT_ROOT"] == str(project)
        assert "DANGO_CLOUD_MODE" in _ANSI_RE.sub("", result.output)  # and the user is told

    def test_cloud_mode_set_in_the_shell_is_kept(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        (project / ".env").write_text("WP35_VAR=from-dotenv\n", encoding="utf-8")
        out = tmp_path / "child_env.json"
        result, _ = _start_with_real_child(project, out)
        assert result.exit_code == 0, result.output
        env = _child_env(out)
        assert env["WP35_VAR"] == "from-dotenv"
        assert env["DANGO_CLOUD_MODE"] == "true"


class TestScriptsSeeDotenv:
    def test_script_started_while_start_runs_sees_the_dotenv_value(self, project: Path) -> None:
        (project / ".env").write_text("WP35_VAR=from-dotenv\n", encoding="utf-8")
        (project / "scripts").mkdir()
        script = project / "scripts" / "show.py"
        script.write_text(_PRINT_SCRIPT, encoding="utf-8")
        seen: list[str] = []

        def server(root: Path, host: str = "", port: int = 0) -> int:
            from dango.platform import script_launch

            started = script_launch.start_script(root, "show.py", script)
            stdout, _ = started.proc.communicate(timeout=30)
            seen.append(stdout.decode("utf-8") if isinstance(stdout, bytes) else str(stdout))
            return 4242

        result = _invoke_start(project, server)
        assert result.exit_code == 0, result.output
        assert seen == ["'from-dotenv'\n"]

    def test_shell_value_wins_for_scripts(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WP35_VAR", "from-shell")
        (project / ".env").write_text("WP35_VAR=from-dotenv\n", encoding="utf-8")
        seen: list[str | None] = []

        def server(root: Path, host: str = "", port: int = 0) -> int:
            seen.append(os.environ.get("WP35_VAR"))
            return 4242

        result = _invoke_start(project, server)
        assert result.exit_code == 0, result.output
        assert seen == ["from-shell"]


class TestUnreadableEnvFile:
    def test_non_utf8_env_aborts_with_a_clear_message(self, project: Path) -> None:
        (project / ".env").write_bytes(b"WP35_VAR=ok\nWP35_OTHER=caf\xe9\n")
        started: list[str] = []
        result = _invoke_start(project, lambda *a, **k: started.append("server") or 1)
        output = " ".join(_ANSI_RE.sub("", result.output).split())
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)  # click.Abort, not a traceback
        assert ".env" in output
        assert "UTF-8" in output
        assert "Traceback" not in output
        assert started == []

    def test_non_utf8_env_sets_nothing_and_starts_nothing(self, project: Path) -> None:
        (project / ".env").write_bytes(b"WP35_VAR=ok\nWP35_OTHER=caf\xe9\n")
        docker = MagicMock()
        result = _invoke_start(
            project, lambda *a, **k: 1, [patch(f"{_STARTUP}.start_docker_services", docker)]
        )
        assert result.exit_code == 1
        docker.assert_not_called()
        assert "WP35_VAR" not in os.environ

    def test_message_escapes_rich_markup_in_the_path(self, tmp_path: Path) -> None:
        root = tmp_path / "[red]proj"
        root.mkdir()
        (root / ".env").write_bytes(b"WP35_OTHER=caf\xe9\n")
        result = _invoke_start(root, lambda *a, **k: 1)
        output = "".join(_ANSI_RE.sub("", result.output).split())
        assert result.exit_code == 1
        assert "[red]proj" in output

    def test_unreadable_env_aborts_with_a_clear_message(self, project: Path) -> None:
        (project / ".env").write_text("WP35_VAR=ok\n", encoding="utf-8")
        denied = PermissionError(13, "Permission denied")
        result = _invoke_start(
            project,
            lambda *a, **k: 1,
            [patch("dotenv.load_dotenv", side_effect=denied)],
        )
        output = " ".join(_ANSI_RE.sub("", result.output).split())
        assert result.exit_code == 1
        assert "Cannot read" in output
        assert "Permission denied" in output
        assert "Traceback" not in output

    def test_unparseable_lines_do_not_stop_start(self, project: Path) -> None:
        (project / ".env").write_text(
            "this is not a variable line\nWP35_VAR=ok\n", encoding="utf-8"
        )
        seen: list[str | None] = []

        def server(root: Path, host: str = "", port: int = 0) -> int:
            seen.append(os.environ.get("WP35_VAR"))
            return 4242

        result = _invoke_start(project, server)
        assert result.exit_code == 0, result.output
        assert seen == ["ok"]


class TestNoEnvFile:
    def test_start_without_a_dotenv_file_is_unchanged(self, project: Path) -> None:
        calls: list[Path] = []

        def server(root: Path, host: str = "", port: int = 0) -> int:
            calls.append(root)
            return 4242

        result = _invoke_start(project, server)
        assert result.exit_code == 0, result.output
        assert calls == [project]
