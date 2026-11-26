"""tests/unit/test_web_serve_auth_off_bind.py

`dango web` and `dango serve` listen on 127.0.0.1 when local auth is explicitly off and no --host is
given; an explicit --host wins; the printed URL and the port check use the host actually bound.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.serve import serve
from dango.cli.commands.web import web

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_SERVE = "dango.cli.commands.serve"
_PROLOGUE = "dango.platform.common.startup_prologue"
_NOTICE = "only on this computer"
_WARNING = "without logging in"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("DANGO_PROJECT_ROOT", "x")  # restored on teardown
    monkeypatch.delenv("DANGO_PROJECT_ROOT")


def _auth(root: Path, text: str | bytes | None) -> Path:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    if text is not None:
        path = root / ".dango" / "auth.yml"
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return root


def _squash(result: Result) -> str:
    return " ".join(_ANSI.sub("", result.output).split())


def _run_web(root: Path, args: list[str]) -> tuple[Result, MagicMock]:
    with (
        patch("dango.cli.utils.require_project_context", return_value=root),
        patch(f"{_PROLOGUE}.run_local_prologue", MagicMock()),
        patch("uvicorn.run") as uvicorn_run,
    ):
        result = CliRunner().invoke(web, args, obj={})
    assert result.exit_code == 0, result.output
    return result, uvicorn_run


def _run_serve(root: Path, args: list[str]) -> tuple[Result, MagicMock, MagicMock]:
    config = MagicMock()
    config.project.name = "p"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    loader = MagicMock()
    loader.load_config.return_value = config
    with ExitStack() as stack:
        stack.enter_context(patch("dango.cli.utils.require_project_context", return_value=root))
        stack.enter_context(patch("dango.config.ConfigLoader", return_value=loader))
        stack.enter_context(patch(f"{_STARTUP}.run_pending_migrations", return_value={}))
        for name in (
            "ensure_dbt_schemas",
            "ensure_duckdb_driver",
            "start_docker_services",
            "setup_metabase_if_needed",
            "import_dashboards",
        ):
            stack.enter_context(patch(f"{_STARTUP}.{name}"))
        stack.enter_context(patch(f"{_SERVE}._stop_docker_quiet"))
        check_port = stack.enter_context(patch(f"{_SERVE}._check_port"))
        uvicorn_run = stack.enter_context(patch("uvicorn.run"))
        result = CliRunner().invoke(serve, args, obj={})
    assert result.exit_code == 0, result.output
    return result, uvicorn_run, check_port


class TestWebBind:
    def test_auth_disabled_binds_loopback_and_says_so_once(self, tmp_path: Path) -> None:
        result, uvicorn_run = _run_web(_auth(tmp_path, "enabled: false\n"), [])
        text = _squash(result)
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert text.count(_NOTICE) == 1
        assert "--host 0.0.0.0" in text
        assert "http://127.0.0.1:8080" in text
        assert "http://0.0.0.0:8080" not in text
        assert _WARNING not in text

    def test_auth_enabled_is_unchanged(self, tmp_path: Path) -> None:
        result, uvicorn_run = _run_web(_auth(tmp_path, "enabled: true\n"), [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert _NOTICE not in _squash(result)
        assert "http://0.0.0.0:8080" in _squash(result)

    @pytest.mark.parametrize("content", [None, b"", b"enabled: [unclosed\n", b"\xff\xfe\x00"])
    def test_a_missing_or_broken_auth_file_is_unchanged(
        self, tmp_path: Path, content: bytes | None
    ) -> None:
        result, uvicorn_run = _run_web(_auth(tmp_path, content), [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert _NOTICE not in _squash(result)

    def test_cloud_mode_is_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        result, uvicorn_run = _run_web(_auth(tmp_path, "enabled: false\n"), [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert _NOTICE not in _squash(result)

    def test_explicit_network_host_wins_with_one_warning(self, tmp_path: Path) -> None:
        result, uvicorn_run = _run_web(
            _auth(tmp_path, "enabled: false\n"), ["--host", "0.0.0.0", "--port", "9001"]
        )
        text = _squash(result)
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert uvicorn_run.call_args.kwargs["port"] == 9001
        assert text.count(_WARNING) == 1
        assert _NOTICE not in text
        assert "http://0.0.0.0:9001" in text

    def test_explicit_loopback_host_is_silent(self, tmp_path: Path) -> None:
        result, uvicorn_run = _run_web(_auth(tmp_path, "enabled: false\n"), ["--host", "127.0.0.1"])
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert _NOTICE not in _squash(result)
        assert _WARNING not in _squash(result)

    def test_explicit_network_host_with_auth_enabled_is_silent(self, tmp_path: Path) -> None:
        result, uvicorn_run = _run_web(_auth(tmp_path, "enabled: true\n"), ["--host", "0.0.0.0"])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert _WARNING not in _squash(result)

    def test_help_describes_the_bind_rule(self) -> None:
        text = " ".join(CliRunner().invoke(web, ["--help"]).output.split())
        assert "127.0.0.1" in text
        assert "authentication is turned off" in text


class TestServeBind:
    def test_auth_disabled_binds_loopback_and_checks_that_host(self, tmp_path: Path) -> None:
        result, uvicorn_run, check_port = _run_serve(_auth(tmp_path, "enabled: false\n"), [])
        text = _squash(result)
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert check_port.call_args.args == (8800, "127.0.0.1")
        assert text.count(_NOTICE) == 1
        assert "--host 0.0.0.0" in text
        assert "Starting Dango on 127.0.0.1:8800" in text

    def test_auth_enabled_is_unchanged(self, tmp_path: Path) -> None:
        result, uvicorn_run, check_port = _run_serve(_auth(tmp_path, "enabled: true\n"), [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert check_port.call_args.args == (8800, "0.0.0.0")
        assert _NOTICE not in _squash(result)

    @pytest.mark.parametrize("content", [None, b"", b"enabled: [unclosed\n", b"\xff\xfe\x00"])
    def test_a_missing_or_broken_auth_file_is_unchanged(
        self, tmp_path: Path, content: bytes | None
    ) -> None:
        result, uvicorn_run, check_port = _run_serve(_auth(tmp_path, content), [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert check_port.call_args.args == (8800, "0.0.0.0")
        assert _NOTICE not in _squash(result)

    def test_cloud_mode_keeps_its_own_loopback_without_the_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        result, uvicorn_run, check_port = _run_serve(_auth(tmp_path, "enabled: false\n"), [])
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert check_port.call_args.args == (8800, "127.0.0.1")
        assert _NOTICE not in _squash(result)

    def test_explicit_network_host_wins_with_one_warning(self, tmp_path: Path) -> None:
        result, uvicorn_run, check_port = _run_serve(
            _auth(tmp_path, "enabled: false\n"), ["--host", "0.0.0.0"]
        )
        text = _squash(result)
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"
        assert check_port.call_args.args == (8800, "0.0.0.0")
        assert text.count(_WARNING) == 1
        assert _NOTICE not in text

    def test_explicit_loopback_host_is_silent(self, tmp_path: Path) -> None:
        result, uvicorn_run, _check = _run_serve(
            _auth(tmp_path, "enabled: false\n"), ["--host", "127.0.0.1"]
        )
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert _NOTICE not in _squash(result)
        assert _WARNING not in _squash(result)

    def test_help_describes_the_bind_rule(self) -> None:
        text = " ".join(CliRunner().invoke(serve, ["--help"]).output.split())
        assert "127.0.0.1" in text
        assert "authentication is turned off" in text


class TestAuthDisableMessage:
    def test_the_message_tells_the_user_to_restart_a_running_server(self, tmp_path: Path) -> None:
        from dango.cli.main import cli

        _auth(tmp_path, "enabled: true\n")
        with patch("dango.cli.utils.find_project_root", return_value=tmp_path):
            result = CliRunner().invoke(cli, ["auth", "disable"], input="y\n")
        text = _squash(result)
        assert result.exit_code == 0, result.output
        assert "restart it so it listens only on this computer" in text
        assert "dango auth enable" in text
