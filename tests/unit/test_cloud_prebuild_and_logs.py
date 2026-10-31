"""tests/unit/test_cloud_prebuild_and_logs.py

1.0.13-T19: remote logs resolves the Metabase container by Compose labels. A recording fake SSH is used.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import mcp_remote

PROJECT_ID = "31dc6a77"
PROJECT = f"dango-{PROJECT_ID}"
PROJECT_YML = f"project:\n  id: {PROJECT_ID}deadbeef\n"
CONTAINER = f"{PROJECT}-metabase-1"
STOP = "stop-after-build"


def _res(stdout: str = "", stderr: str = "", code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=code, success=code == 0)


class RecordingSSH:
    """Fake SSH: records every command, answers project.yml, never touches a network."""

    def __init__(
        self,
        containers: str = CONTAINER,
        label_hit: bool = True,
        stopped: str | None = None,
        project_yml: str = PROJECT_YML,
    ) -> None:
        self.stopped = stopped
        self.project_yml = project_yml
        self.commands: list[str] = []
        self.containers = containers
        self.label_hit = label_hit
        self.disconnected = False

    def connect(self, *a: Any, **k: Any) -> RecordingSSH:
        return self

    def disconnect(self) -> None:
        self.disconnected = True

    def __getattr__(self, name: str) -> Any:
        """Non-command SSH helpers (key generation, file writes) are inert mocks."""
        return MagicMock(name=name)

    def exec_command(self, cmd: str, **kw: Any) -> SimpleNamespace:
        self.commands.append(cmd)
        if cmd.startswith('[ -n "$(find ') and "-user root" in cmd:
            return _res("", "", 1)  # the compose-helper marker is absent: legacy build
        if "cat" in cmd and "project.yml" in cmd:
            return _res(self.project_yml)
        if cmd.startswith("docker ps"):
            if "label=" in cmd and not self.label_hit:
                return _res("")
            if self.stopped is not None and " -a " in cmd:
                return _res(self.stopped)
            return _res(self.containers)
        return _res("4")


@pytest.fixture
def mcp_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = SimpleNamespace(droplet_ip="203.0.113.7", ssh_key_path="k", deploy_branch="main")
    monkeypatch.setattr(mcp_remote, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_remote, "_cloud", lambda _r: cfg)
    state = SimpleNamespace(ssh=RecordingSSH())
    with patch("dango.platform.cloud.ssh.SSHManager", side_effect=lambda **_k: state.ssh):
        yield state


@pytest.mark.unit
class TestMetabaseLogsMcp:
    def test_row2_resolves_then_reads(self, mcp_ssh: Any) -> None:
        out = mcp_remote.remote_logs(service="metabase", lines=7)
        cmds = mcp_ssh.ssh.commands
        ps = next(c for c in cmds if c.startswith("docker ps"))
        assert f"label=com.docker.compose.project={PROJECT}" in ps
        assert "label=com.docker.compose.service=metabase" in ps
        assert cmds[-1] == f"docker logs --tail 7 {CONTAINER}"
        assert "error" not in out and out["service"] == "metabase"

    def test_row3_no_container_is_clear_error(self, mcp_ssh: Any) -> None:
        mcp_ssh.ssh = RecordingSSH(containers="")
        out = mcp_remote.remote_logs(service="metabase")
        assert out == {"error": "No metabase container found on the server."}
        assert not any(c.startswith("docker logs") for c in mcp_ssh.ssh.commands)

    def test_name_filter_fallback(self, mcp_ssh: Any) -> None:
        mcp_ssh.ssh = RecordingSSH(label_hit=False)
        mcp_remote.remote_logs(service="metabase", lines=3)
        assert mcp_ssh.ssh.commands[-1] == f"docker logs --tail 3 {CONTAINER}"

    @pytest.mark.parametrize(
        "other", ["dango-ffffffff-metabase-1", "metabase-proxy", "pg-metabase-db", "x; id"]
    )
    def test_other_containers_rejected(self, mcp_ssh: Any, other: str) -> None:
        mcp_ssh.ssh = RecordingSSH(containers=other)
        out = mcp_remote.remote_logs(service="metabase")
        assert out == {"error": "No metabase container found on the server."}
        assert not any(c.startswith("docker logs") for c in mcp_ssh.ssh.commands)

    def test_running_preferred_then_all(self, mcp_ssh: Any) -> None:
        mcp_ssh.ssh = RecordingSSH()
        mcp_remote.remote_logs(service="metabase")
        ps = [c for c in mcp_ssh.ssh.commands if c.startswith("docker ps")]
        assert ps and " -a " not in ps[0]
        mcp_ssh.ssh = RecordingSSH(containers="", stopped=CONTAINER)
        mcp_remote.remote_logs(service="metabase")
        assert mcp_ssh.ssh.commands[-1] == f"docker logs --tail 100 {CONTAINER}"

    def test_legacy_project_name(self, mcp_ssh: Any) -> None:
        from dango.platform.cloud.backup import _legacy_path_hash

        legacy = f"dango-{_legacy_path_hash('/srv/dango/project')}-metabase-1"
        mcp_ssh.ssh = RecordingSSH(containers=legacy, project_yml="")
        mcp_remote.remote_logs(service="metabase")
        assert mcp_ssh.ssh.commands[-1] == f"docker logs --tail 100 {legacy}"

    def test_hostile_container_name_is_not_used(self, mcp_ssh: Any) -> None:
        mcp_ssh.ssh = RecordingSSH(containers="x; id")
        out = mcp_remote.remote_logs(service="metabase")
        assert "error" in out
        assert not any("; id" in c and c.startswith("docker logs") for c in mcp_ssh.ssh.commands)

    def test_row4_other_services_unchanged(self, mcp_ssh: Any) -> None:
        mcp_remote.remote_logs(service="dango", lines=9)
        mcp_remote.remote_logs(service="caddy", lines=9)
        assert mcp_ssh.ssh.commands == [
            "journalctl -u dango-web --no-pager -n 9",
            "journalctl -u caddy --no-pager -n 9",
        ]

    def test_row5_hostile_and_clamped(self, mcp_ssh: Any) -> None:
        assert "error" in mcp_remote.remote_logs(service="metabase; id")
        assert "error" in mcp_remote.remote_logs(service="dbt-docs")
        assert mcp_ssh.ssh.commands == []
        mcp_remote.remote_logs(service="dango", lines=99999)
        assert mcp_ssh.ssh.commands[-1].endswith("-n 500")
        mcp_remote.remote_logs(service="dango", lines=-4)
        assert mcp_ssh.ssh.commands[-1].endswith("-n 1")


@pytest.mark.unit
class TestMetabaseLogsCli:
    def _invoke(self, ssh: RecordingSSH, args: list[str], tmp_path: Path) -> Any:
        from dango.cli.commands.remote import remote

        cfg = MagicMock(droplet_ip="203.0.113.7")
        loader = MagicMock()
        loader.load_cloud_config.return_value = cfg
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.loader.ConfigLoader", return_value=loader),
            patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        ):
            return CliRunner().invoke(
                remote, ["logs", *args], obj={"project_root": tmp_path}, catch_exceptions=False
            )

    def test_row2_cli_resolves_then_reads(self, tmp_path: Path) -> None:
        ssh = RecordingSSH()
        res = self._invoke(ssh, ["--service", "metabase", "--tail", "20"], tmp_path)
        assert res.exit_code == 0, res.output
        assert ssh.commands[-1] == f"docker logs --tail 20 {CONTAINER}"

    def test_row3_cli_no_container(self, tmp_path: Path) -> None:
        ssh = RecordingSSH(containers="")
        res = self._invoke(ssh, ["--service", "metabase"], tmp_path)
        assert res.exit_code == 1
        assert "No metabase container found" in res.output
        assert ssh.disconnected

    def test_row4_cli_caddy_unchanged(self, tmp_path: Path) -> None:
        ssh = RecordingSSH()
        res = self._invoke(ssh, ["--service", "caddy", "--tail", "20"], tmp_path)
        assert res.exit_code == 0, res.output
        assert ssh.commands == ["journalctl -u caddy --no-pager -n 20"]
