"""tests/unit/test_health_consumers.py

Every in-repo consumer of GET /api/health keeps working now that it reports Dango, not Metabase.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.platform.cloud.ssh import CommandResult

_SRC = Path(__file__).resolve().parents[2] / "dango"


def _body(metabase: str) -> str:
    return json.dumps(
        {"status": "ok", "components": {"dango": "ok", "metabase": metabase, "docker": "ok"}}
    )


class _ScriptedSSH:
    """Fake SSH whose ``curl`` answers come from a script; the last answer repeats."""

    def __init__(self, *answers: CommandResult) -> None:
        self.answers = list(answers)
        self.commands: list[str] = []

    def exec_command(self, cmd: str, timeout: int = 0) -> CommandResult:
        self.commands.append(cmd)
        index = min(len(self.commands) - 1, len(self.answers) - 1)
        return self.answers[index]


def _ok(body: str = "") -> CommandResult:
    return CommandResult(stdout=body, stderr="", exit_code=0)


def _refused() -> CommandResult:
    return CommandResult(stdout="", stderr="curl: (7) refused", exit_code=7)


@pytest.fixture
def dango_only_server() -> Iterator[str]:
    """A local Dango-only double: answers 200 with Metabase reported unavailable."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = _body("unavailable").encode()
            self.send_response(200 if self.path == "/api/health" else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.unit
def test_deploy_health_check_succeeds_while_metabase_is_down(dango_only_server: str) -> None:
    from dango.cli.commands.deploy_provision import _health_check

    with patch("dango.cli.commands.deploy_provision.time.sleep") as sleep:
        assert _health_check(dango_only_server, timeout=10, interval=5) is True
    sleep.assert_not_called()


@pytest.mark.unit
def test_backup_verify_health_waits_for_dango_only() -> None:
    from dango.platform.cloud.backup import verify_health

    ssh = _ScriptedSSH(_ok(_body("unavailable")))
    with patch("dango.platform.cloud.backup.time.sleep") as sleep:
        assert verify_health(ssh, timeout=90) is True
    assert len(ssh.commands) == 1
    sleep.assert_not_called()


@pytest.mark.unit
def test_backup_verify_health_still_a_status_only_probe_for_any_body() -> None:
    from dango.platform.cloud.backup import verify_health

    with patch("dango.platform.cloud.backup.time.sleep"):
        assert verify_health(_ScriptedSSH(_ok("not json")), timeout=90) is True
        assert verify_health(_ScriptedSSH(_ok("")), timeout=90) is True


@pytest.mark.unit
def test_verify_health_can_require_metabase_and_waits_through_starting() -> None:
    from dango.platform.cloud.backup import verify_health

    ssh = _ScriptedSSH(_ok(_body("starting")), _ok(_body("unavailable")), _ok(_body("ok")))
    with patch("dango.platform.cloud.backup.time.sleep") as sleep:
        assert verify_health(ssh, timeout=90, require_metabase=True) is True
    assert len(ssh.commands) == 3
    assert sleep.call_count == 2


@pytest.mark.unit
def test_verify_health_require_metabase_times_out_when_metabase_never_comes_up() -> None:
    from dango.platform.cloud.backup import verify_health

    ssh = _ScriptedSSH(_ok(_body("unavailable")))
    with (
        patch("dango.platform.cloud.backup.time.sleep"),
        patch("dango.platform.cloud.backup.time.monotonic", side_effect=[0.0, 1.0, 2.0, 100.0]),
    ):
        assert verify_health(ssh, timeout=90, require_metabase=True) is False
    assert len(ssh.commands) == 2


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", '{"status":"ok"}', "not json"])
def test_verify_health_require_metabase_accepts_a_server_that_still_proxies_metabase(
    body: str,
) -> None:
    """An older server (not yet upgraded) answers with Metabase's own body: 200 meant Metabase was up."""
    from dango.platform.cloud.backup import verify_health

    with patch("dango.platform.cloud.backup.time.sleep"):
        assert verify_health(_ScriptedSSH(_ok(body)), timeout=90, require_metabase=True) is True


@pytest.mark.unit
def test_verify_health_keeps_polling_while_dango_is_down() -> None:
    from dango.platform.cloud.backup import verify_health

    ssh = _ScriptedSSH(_refused(), _refused(), _ok(_body("ok")))
    with patch("dango.platform.cloud.backup.time.sleep"):
        assert verify_health(ssh, timeout=90) is True
    assert len(ssh.commands) == 3


def _run_verify_metabase(result: CommandResult, tmp_path: Path) -> Any:
    ssh = MagicMock()
    ssh.exec_command.return_value = result
    with patch(
        "dango.cli.commands.remote_backup._load_cloud_config_with_ssh_or_fail",
        return_value=(MagicMock(), ssh),
    ):
        out = CliRunner().invoke(
            remote, ["backup", "verify-metabase"], obj={"project_root": tmp_path}
        )
    ssh.disconnect.assert_called_once()
    return out


@pytest.mark.unit
def test_verify_metabase_live_passes_when_the_component_is_ok(tmp_path: Path) -> None:
    out = _run_verify_metabase(_ok(_body("ok")), tmp_path)
    assert out.exit_code == 0
    assert "PASS" in out.output and "FAIL" not in out.output
    assert "Metabase is responding" in out.output


@pytest.mark.unit
@pytest.mark.parametrize("state", ["starting", "unavailable", "unknown"])
def test_verify_metabase_live_fails_when_dango_is_up_but_metabase_is_not(
    tmp_path: Path, state: str
) -> None:
    out = _run_verify_metabase(_ok(_body(state)), tmp_path)
    assert out.exit_code == 0
    assert "FAIL" in out.output and "PASS" not in out.output
    assert "Metabase is responding" not in out.output
    assert "Dango" in out.output


@pytest.mark.unit
def test_verify_metabase_live_fails_with_a_true_message_when_dango_is_down(
    tmp_path: Path,
) -> None:
    out = _run_verify_metabase(_refused(), tmp_path)
    assert "FAIL" in out.output and "PASS" not in out.output
    assert "Metabase is responding" not in out.output


@pytest.mark.unit
def test_verify_metabase_live_older_server_body_keeps_working(tmp_path: Path) -> None:
    """An older server proxies Metabase's own ``{"status":"ok"}``: that still means Metabase is up."""
    out = _run_verify_metabase(_ok('{"status":"ok"}'), tmp_path)
    assert "PASS" in out.output and "FAIL" not in out.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        "platform/cloud/backup.py",
        "platform/cloud/upgrade.py",
        "platform/cloud/migrate.py",
        "platform/cloud/resize.py",
        "cli/commands/remote_power.py",
    ],
)
def test_post_restart_flows_that_need_metabase_ask_for_it_explicitly(module: str) -> None:
    """Callers that report 'healthy' to a user (or delete the old server) still require Metabase."""
    tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "verify_health"
    ]
    assert calls, module
    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert isinstance(keywords.get("require_metabase"), ast.Constant), (module, call.lineno)
        assert keywords["require_metabase"].value is True, (module, call.lineno)


@pytest.mark.unit
def test_verify_metabase_live_when_health_does_not_answer_makes_no_false_claim(
    tmp_path: Path,
) -> None:
    """An older server proxies Metabase, so a refused request may mean Dango is up and Metabase down."""
    out = _run_verify_metabase(_refused(), tmp_path)
    text = " ".join(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.output).split())
    assert "web server is not responding" not in text
    assert "/api/health did not answer" in text
