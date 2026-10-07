"""tests/unit/test_cloud_prebuild_and_logs.py

1.0.13-T19: the deploy pre-build passes COMPOSE_PROJECT_NAME through sudo via env, and
remote logs resolves the Metabase container by Compose labels. A recording fake SSH is used.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import deploy_provision, mcp_remote
from dango.cli.commands.deploy_provision import _prebuild_command
from dango.cli.commands.deploy_wizard import BYOSConfig, WizardConfig
from dango.exceptions import CloudProvisioningError

PROJECT_ID = "31dc6a77"
PROJECT = f"dango-{PROJECT_ID}"
PROJECT_YML = f"project:\n  id: {PROJECT_ID}deadbeef\n"
CONTAINER = f"{PROJECT}-metabase-1"
STOP = "stop-after-build"


def _res(stdout: str = "", stderr: str = "", code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=code, success=code == 0)


class RecordingSSH:
    """Fake SSH: records every command, answers project.yml, never touches a network."""

    def __init__(self, containers: str = CONTAINER, label_hit: bool = True) -> None:
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
        if cmd.startswith("cat ") and cmd.endswith("project.yml"):
            return _res(PROJECT_YML)
        if cmd.startswith("docker ps"):
            if "label=" in cmd and not self.label_hit:
                return _res("")
            return _res(self.containers)
        return _res("4")


@pytest.mark.unit
class TestPrebuildCommand:
    @pytest.mark.parametrize("name", [PROJECT, "x; rm -rf /", "a b$(id)"])
    def test_env_form_name_is_one_token(self, name: str) -> None:
        tokens = shlex.split(_prebuild_command(name).split("&& ", 1)[1])
        assert tokens == [
            "sudo",
            "-u",
            "dango",
            "env",
            f"COMPOSE_PROJECT_NAME={name}",
            "docker",
            "compose",
            "build",
        ]

    def test_no_var_before_sudo_form_in_module(self) -> None:
        source = Path(deploy_provision.__file__).read_text()
        assert not re.search(r"&&\s*COMPOSE_PROJECT_NAME=\S+\s+\"?\s*sudo", source)
        assert not re.search(r"COMPOSE_PROJECT_NAME=\{?\w+\}?\s+sudo", source)

    def _build_cmd(self, ssh: RecordingSSH) -> str:
        builds = [c for c in ssh.commands if c.endswith("docker compose build")]
        assert len(builds) == 1, ssh.commands
        return builds[0]

    def test_do_flow_uses_env_form(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        (tmp_path / ".dango").mkdir()
        config = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="a@example.com",
            admin_password="strongpassword123",
            skip_oauth=True,
            enable_backups=False,
            monthly_cost=24,
        )
        droplet = {"id": 2, "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]}}
        client = MagicMock()
        client.upload_ssh_key.return_value = {"id": 1}
        ssh = RecordingSSH()
        with (
            patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
            patch("dango.platform.cloud.provisioning.provision_droplet", return_value=droplet),
            patch(
                "dango.platform.cloud.firewall.create_default_firewall", return_value={"id": "fw"}
            ),
            patch("dango.platform.cloud.server_setup.setup_server"),
            patch("dango.platform.cloud.file_sync.sync_project_files"),
            patch("dango.platform.cloud.provisioning.save_provisioning_metadata"),
            patch("dango.cli.commands.deploy_wizard._safe_confirm", return_value=True),
            patch.object(deploy_provision, "_start_services", side_effect=RuntimeError(STOP)),
            patch.object(deploy_provision, "time"),
            patch.object(deploy_provision, "console", MagicMock()),
            pytest.raises(Exception) as ei,  # noqa: B017 - flow wraps the sentinel
        ):
            deploy_provision.run_provisioning(tmp_path, config)
        assert STOP in str(ei.value)  # flow reached service start, i.e. the build ran
        cmd = self._build_cmd(ssh)
        assert f"sudo -u dango env COMPOSE_PROJECT_NAME={PROJECT} docker compose build" in cmd

    def test_byos_flow_uses_env_form(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        key = tmp_path / "key"
        key.write_text("k")
        config = BYOSConfig(
            server_ip="203.0.113.9",
            ssh_user="root",
            ssh_key_path=str(key),
            domain=None,
            admin_email="a@example.com",
            admin_password="strongpassword123",
            skip_oauth=True,
            push_secrets=False,
        )
        ssh = RecordingSSH()
        with (
            patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
            patch("dango.platform.cloud.server_setup.setup_server"),
            patch("dango.platform.cloud.file_sync.sync_project_files"),
            patch("dango.cli.commands.deploy_wizard._safe_confirm", return_value=True),
            patch.object(deploy_provision, "_start_services", side_effect=RuntimeError(STOP)),
            patch.object(deploy_provision, "time"),
            patch.object(deploy_provision, "console", MagicMock()),
            pytest.raises((RuntimeError, CloudProvisioningError)) as ei,
        ):
            deploy_provision.run_byos_setup(tmp_path, config, non_interactive=True)
        assert STOP in str(ei.value)  # flow reached service start, i.e. the build ran
        cmd = self._build_cmd(ssh)
        assert f"sudo -u dango env COMPOSE_PROJECT_NAME={PROJECT} docker compose build" in cmd


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
        assert out == {"error": "Could not read metabase logs: no metabase container found"}
        assert not any(c.startswith("docker logs") for c in mcp_ssh.ssh.commands)

    def test_name_filter_fallback(self, mcp_ssh: Any) -> None:
        mcp_ssh.ssh = RecordingSSH(label_hit=False)
        mcp_remote.remote_logs(service="metabase", lines=3)
        assert mcp_ssh.ssh.commands[-1] == f"docker logs --tail 3 {CONTAINER}"

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
