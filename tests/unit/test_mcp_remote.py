"""tests/unit/test_mcp_remote.py

Tests for the MCP remote tools (dango/cli/commands/mcp_remote.py). SSH is mocked.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import mcp_remote


def _res(stdout: str = "", stderr: str = "", code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=code, success=code == 0)


CFG = SimpleNamespace(
    droplet_ip="203.0.113.7", ssh_key_path=".dango/cloud_key", deploy_branch="main"
)


class FakeSSH:
    def __init__(self, handler: Any = None) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.handler = handler or (lambda cmd: _res())
        self.disconnected = False

    def connect(self, *a: Any, **k: Any) -> FakeSSH:
        return self

    def disconnect(self) -> None:
        self.disconnected = True

    def exec_command(self, cmd: str, **kw: Any) -> SimpleNamespace:
        self.commands.append((cmd, kw))
        return self.handler(cmd)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mcp_remote, "_get_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def deployed(root: Path, monkeypatch: pytest.MonkeyPatch):
    """Cloud config present; SSHManager patched to return a FakeSSH (set .ssh to customize)."""
    monkeypatch.setattr(mcp_remote, "_cloud", lambda _r: CFG)
    state = SimpleNamespace(ssh=FakeSSH())
    with patch("dango.platform.cloud.ssh.SSHManager", side_effect=lambda **_k: state.ssh):
        yield state


@pytest.mark.unit
def test_no_cloud_config_returns_error(root: Path) -> None:
    err = {"error": "No cloud deployment configured — run `dango deploy` (CLI) first"}
    assert mcp_remote.remote_status() == err
    assert mcp_remote.remote_logs() == err
    assert mcp_remote.remote_history() == err
    assert mcp_remote.remote_push() == err
    assert mcp_remote.remote_sync("anything") == err
    assert mcp_remote.remote_query("SELECT 1") == err


@pytest.mark.unit
def test_connection_failure_returns_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_remote, "_cloud", lambda _r: CFG)
    boom = MagicMock()
    boom.return_value.connect.side_effect = OSError("refused")
    with patch("dango.platform.cloud.ssh.SSHManager", boom):
        assert mcp_remote.remote_status() == {"error": "Cannot connect to server: refused"}


@pytest.mark.unit
def test_remote_status_serializes(deployed: Any) -> None:
    from dango.platform.cloud.server_status import ServerStatus, ServiceInfo

    st = ServerStatus(cpu_usage_pct=1.5, services=[ServiceInfo("dango-web", "running")])
    with patch("dango.platform.cloud.server_status.collect_server_status", return_value=st):
        out = mcp_remote.remote_status()
    assert out["services"] == [{"name": "dango-web", "status": "running"}]
    json.dumps(out)
    assert deployed.ssh.disconnected


@pytest.mark.unit
def test_remote_logs_redacts_and_caps(deployed: Any) -> None:
    noisy = "\n".join(
        ["\x1b[31mclient_secret=abcSEC\x1b[0m", "auth Bearer tokSEC12345"]
        + [f"line {i} " + "x" * 1500 for i in range(400)]
    )
    deployed.ssh = FakeSSH(lambda cmd: _res(stdout=noisy))
    out = mcp_remote.remote_logs(lines=9999)
    assert deployed.ssh.commands[0][0] == "journalctl -u dango-web --no-pager -n 500"
    assert out["truncated"] is True
    assert sum(len(x) + 1 for x in out["lines"]) <= 200_000
    assert out["lines"][-1].startswith("line 399")

    deployed.ssh = FakeSSH(lambda cmd: _res(stdout="client_secret=abcSEC\nBearer tokSEC12345"))
    out = mcp_remote.remote_logs(service="metabase", lines=0)
    assert deployed.ssh.commands[0][0] == "docker logs metabase --tail 1"
    blob = json.dumps(out)
    assert "abcSEC" not in blob and "tokSEC12345" not in blob and "\x1b" not in blob

    assert "error" in mcp_remote.remote_logs(service="nope")


@pytest.mark.unit
def test_remote_history_limit_clamped(deployed: Any) -> None:
    with patch(
        "dango.platform.cloud.deploy_journal.read_remote_journal", return_value=[{"a": 1}]
    ) as m:
        assert mcp_remote.remote_history(limit=10_000) == [{"a": 1}]
        assert m.call_args.kwargs == {"limit": 100, "raise_on_error": True}
        mcp_remote.remote_history(limit=-5)
        assert m.call_args.kwargs["limit"] == 1


@pytest.mark.unit
def test_remote_query_rejects_non_select_without_connecting(deployed: Any) -> None:
    deployed.ssh = None  # any connection attempt would return None and fail loudly
    with patch("dango.platform.cloud.ssh.SSHManager") as ctor:
        assert "error" in mcp_remote.remote_query("DROP TABLE x")
        assert "error" in mcp_remote.remote_query("SELECT 1" + " " * 102_400)
        ctor.assert_not_called()


_QUERY_OUT = json.dumps(
    {
        "columns": ["id", "email", "phone"],
        "rows": [[1, "a@x.com", "555-0100"]],
        "row_count": 1,
        "truncated": False,
    }
)


def _query_handler(pii_reply: SimpleNamespace):
    def h(cmd: str) -> SimpleNamespace:
        return _res(stdout=_QUERY_OUT) if "remote-query-temp" in cmd else pii_reply

    return h


@pytest.mark.unit
def test_remote_query_masks_union_of_local_and_remote_pii(
    deployed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.commands import mcp_governance

    monkeypatch.setattr(mcp_governance, "_mcp_pii_mask_columns", lambda _r: {"email"})
    deployed.ssh = FakeSSH(_query_handler(_res(stdout='["phone"]')))
    out = mcp_remote.remote_query("SELECT * FROM t")
    assert out["pii_masking"]["masked_columns"] == ["email", "phone"]
    blob = json.dumps(out)
    assert "a@x.com" not in blob and "555-0100" not in blob
    # both remote commands run as the dango user from the project dir
    for cmd, _ in deployed.ssh.commands:
        assert cmd.startswith(
            "cd /srv/dango/project && sudo -u dango -H /srv/dango/venv/bin/python3 -c "
        )
    q = deployed.ssh.commands[0]
    assert q[1]["timeout"] == 40 and q[0].endswith(f" {shlex.quote('SELECT * FROM t')} 30")


@pytest.mark.unit
def test_remote_query_falls_back_to_local_pii_when_remote_unavailable(
    deployed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.commands import mcp_governance

    monkeypatch.setattr(mcp_governance, "_mcp_pii_mask_columns", lambda _r: {"email"})
    deployed.ssh = FakeSSH(_query_handler(_res(stderr="No module", code=1)))
    out = mcp_remote.remote_query("SELECT * FROM t")
    assert out["pii_masking"]["masked_columns"] == ["email"]
    assert "Server PII findings unavailable" in out["pii_masking"]["note"]
    assert out["rows"][0][2] == "555-0100"


@pytest.mark.unit
def test_remote_query_unmasked_when_opted_out(
    deployed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.commands import mcp_governance

    monkeypatch.setattr(mcp_governance, "_mcp_pii_mask_columns", lambda _r: None)
    deployed.ssh = FakeSSH(_query_handler(_res()))
    out = mcp_remote.remote_query("SELECT * FROM t", timeout=999)
    assert "pii_masking" not in out and out["rows"][0][1] == "a@x.com"
    assert deployed.ssh.commands[0][1]["timeout"] == 130
    assert len(deployed.ssh.commands) == 1


@pytest.mark.unit
def test_remote_query_server_error(deployed: Any) -> None:
    deployed.ssh = FakeSSH(lambda c: _res(stderr='{"detail": "bad sql password=hunter2"}', code=1))
    out = mcp_remote.remote_query("SELECT 1")
    assert out == {"error": "bad sql password=****"}


@pytest.mark.unit
def test_remote_sync_unknown_local_source(deployed: Any, root: Path, sample_config) -> None:
    from dango.config.helpers import save_config

    save_config(sample_config, root)
    out = mcp_remote.remote_sync("nope")
    assert "not found" in out["error"]
    assert deployed.ssh.commands == []
    name = sample_config.sources.sources[0].name
    assert "error" in mcp_remote.remote_sync(name, backfill="xyz")


@pytest.fixture
def known_source(root: Path, sample_config) -> str:
    from dango.config.helpers import save_config

    save_config(sample_config, root)
    return sample_config.sources.sources[0].name


@pytest.mark.unit
def test_remote_sync_builds_cli_command(deployed: Any, known_source: str) -> None:
    deployed.ssh = FakeSSH(lambda c: _res(stdout='{"status": "success", "duration_seconds": 3}'))
    out = mcp_remote.remote_sync(known_source, full_refresh=True, backfill="2w")
    payload = {
        "sources": [known_source],
        "full_refresh": True,
        "project_root": "/srv/dango/project",
        "backfill_days": 14,
    }
    expected = (
        "cd /srv/dango/project && sudo -u dango -H env DANGO_CLOUD_MODE=true"
        " /srv/dango/venv/bin/python3 -m dango.platform.scheduling.sync_trigger "
        + shlex.quote(json.dumps(payload))
    )
    assert deployed.ssh.commands == [(expected, {"timeout": 3600, "check": False})]
    assert out["status"] == "success"

    deployed.ssh = FakeSSH(lambda c: _res(stderr="boom", code=1))
    assert mcp_remote.remote_sync(known_source) == {"status": "failed", "error": "boom"}


@pytest.mark.unit
def test_remote_sync_no_wait_uses_background_launcher(deployed: Any, known_source: str) -> None:
    out = mcp_remote.remote_sync(known_source, wait=False)
    cmd, kw = deployed.ssh.commands[0]
    assert cmd.startswith("sh -c ")
    script = shlex.split(cmd)[2]
    assert script.startswith("cd /srv/dango/project || {")
    assert "\nnohup sudo -u dango -H env DANGO_CLOUD_MODE=true" in script
    assert "nohup cd" not in script and "> /dev/null 2>&1 &\n" in script
    assert kw == {"timeout": 30, "check": False}
    assert out == {"status": "started", "source": known_source}


@pytest.mark.unit
def test_remote_push_requires_confirm_without_connecting(deployed: Any) -> None:
    with patch("dango.platform.cloud.ssh.SSHManager") as ctor:
        out = mcp_remote.remote_push(dry_run=False)
        assert "explicitly approves" in out["error"]
        ctor.assert_not_called()


def _deploy_result(dry_run: bool) -> Any:
    from dango.platform.cloud.deployer import DeployResult
    from dango.platform.cloud.file_sync import SyncResult

    return DeployResult(
        sync_result=SyncResult(synced_files=["a.yml"] * 300, added_models=["m1"], dry_run=dry_run),
        dry_run=dry_run,
    )


def _git(clean: bool = True, branch: str = "main") -> Any:
    from dango.utils.git_info import GitInfo

    return GitInfo(commit_sha="abc", branch=branch, is_clean=clean, is_git_repo=True)


@pytest.mark.unit
def test_remote_push_guardrail_failure_blocks(deployed: Any) -> None:
    with (
        patch("dango.utils.git_info.collect_git_info", return_value=_git(clean=False)),
        patch("dango.platform.cloud.deployer.push_deploy") as push,
    ):
        out = mcp_remote.remote_push(dry_run=False, confirm=True)
    assert out["error"] == "Git guardrails failed"
    assert "Working tree has uncommitted changes." in out["errors"]
    push.assert_not_called()
    assert deployed.ssh.commands == []


@pytest.mark.unit
@pytest.mark.parametrize("dry_run,confirm", [(True, False), (False, True)])
def test_remote_push_never_forces(deployed: Any, dry_run: bool, confirm: bool) -> None:
    with (
        patch("dango.utils.git_info.collect_git_info", return_value=_git()),
        patch(
            "dango.platform.cloud.deployer.push_deploy", return_value=_deploy_result(dry_run)
        ) as push,
    ):
        out = mcp_remote.remote_push(dry_run=dry_run, confirm=confirm)
    assert push.call_args.kwargs["force"] is False
    assert push.call_args.kwargs["dry_run"] is dry_run
    assert out["dry_run"] is dry_run and out["added_models"] == ["m1"]
    assert out["files_synced_count"] == 300 and len(out["files_synced"]) == 200
    json.dumps(out)


@pytest.mark.unit
def test_remote_push_failure_mentions_backup_and_redacts(deployed: Any) -> None:
    with (
        patch("dango.utils.git_info.collect_git_info", return_value=_git()),
        patch(
            "dango.platform.cloud.deployer.push_deploy", side_effect=RuntimeError("token=sekrit")
        ),
    ):
        out = mcp_remote.remote_push(dry_run=False, confirm=True)
    assert "sekrit" not in out["error"] and "rollback" in out["error"]


@pytest.mark.unit
def test_remote_tools_registered() -> None:
    from dango.cli.commands import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {
        "remote_status",
        "remote_logs",
        "remote_history",
        "remote_query",
        "remote_sync",
        "remote_push",
    } <= names


@pytest.mark.unit
@pytest.mark.parametrize(
    ("res", "expected"),
    [
        (_res(stderr="cannot cd to /srv/dango/project", code=2), "cannot cd to /srv/dango/project"),
        (_res(stderr="command exited early with status 7", code=3), "status 7"),
        (_res(stderr="", code=3), "Could not start sync"),
    ],
)
def test_remote_sync_no_wait_reports_start_failure(
    deployed: Any, known_source: str, res: Any, expected: str
) -> None:
    deployed.ssh = FakeSSH(lambda c: res)
    out = mcp_remote.remote_sync(known_source, wait=False)
    assert out["status"] == "failed" and expected in out["error"]


@pytest.mark.unit
def test_remote_sync_no_wait_started_when_launcher_succeeds(
    deployed: Any, known_source: str
) -> None:
    deployed.ssh = FakeSSH(lambda c: _res(stdout="started (finished within 2s)\n"))
    assert mcp_remote.remote_sync(known_source, wait=False)["status"] == "started"


@pytest.mark.unit
def test_remote_sync_no_wait_ssh_error_is_reported_not_raised(
    deployed: Any, known_source: str
) -> None:
    from dango.exceptions import CloudSSHError

    def boom(cmd: str) -> Any:
        raise CloudSSHError("channel timed out")

    deployed.ssh = FakeSSH(boom)
    assert "channel timed out" in mcp_remote.remote_sync(known_source, wait=False)["error"]


@pytest.mark.unit
def test_remote_logs_failed_command_is_error(deployed: Any) -> None:
    deployed.ssh = FakeSSH(lambda c: _res(stderr="No such container", code=1))
    assert "error" in mcp_remote.remote_logs(service="metabase")


@pytest.mark.unit
def test_remote_push_real_blocks_when_tree_state_unknown(deployed: Any) -> None:
    from dango.utils.git_info import GitInfo

    gi = GitInfo(commit_sha="a", branch="main", is_clean=None, is_git_repo=True)
    with (
        patch("dango.utils.git_info.collect_git_info", return_value=gi),
        patch(
            "dango.platform.cloud.deployer.push_deploy", return_value=_deploy_result(True)
        ) as push,
    ):
        assert (
            mcp_remote.remote_push(dry_run=False, confirm=True)["error"] == "Git guardrails failed"
        )
        push.assert_not_called()
        assert "error" not in mcp_remote.remote_push(dry_run=True)


@pytest.mark.unit
def test_remote_push_non_git_proceeds_with_warning(deployed: Any) -> None:
    from dango.utils.git_info import GitInfo

    with (
        patch("dango.utils.git_info.collect_git_info", return_value=GitInfo()),
        patch(
            "dango.platform.cloud.deployer.push_deploy", return_value=_deploy_result(True)
        ) as push,
    ):
        out = mcp_remote.remote_push()
    assert push.call_args.kwargs["git_info"] is None and "skipped" in out["warnings"][0]


@pytest.mark.unit
def test_failed_operation_still_disconnects(deployed: Any) -> None:
    with patch(
        "dango.platform.cloud.deploy_journal.read_remote_journal", side_effect=RuntimeError("x")
    ):
        assert "error" in mcp_remote.remote_history()
    assert deployed.ssh.disconnected
