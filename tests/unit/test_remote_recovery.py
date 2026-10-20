"""tests/unit/test_remote_recovery.py

Recovery from an interrupted `remote upgrade` / `remote resize`: the server-side upgrade guard,
power-state helpers, `remote power-on`, and the resize cleanup that no longer swallows errors.
"""

from __future__ import annotations

import itertools
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudAuthError, CloudError, CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult

_PATCH_BACKUP = "dango.platform.cloud.backup.create_backup"
_PATCH_WAIT_DROPLET = "dango.platform.cloud.provisioning.wait_for_droplet_ready"
_PATCH_WAIT_SSH = "dango.platform.cloud.provisioning.wait_for_ssh"
_PATCH_MAKE_CLIENT = "dango.cli.commands.remote._make_client"
_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_SSH_CLS = "dango.platform.cloud.ssh.SSHManager"
_GUARD = "dango-upgrade-guard"
_PIP = "/srv/dango/venv/bin/pip install"


@pytest.fixture(autouse=True)
def _trusted_server_venv() -> Any:
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


class _RecordingSSH:
    """Fake root SSH that records every command and can fail chosen ones."""

    def __init__(self, *, fail: tuple[str, ...] = (), existing_units: tuple[str, ...] = ()) -> None:
        self.cmds: list[str] = []
        self.fail = fail
        self.units = set(existing_units)

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.cmds.append(command)
        if command.startswith("systemd-run"):
            unit = re.search(r"--unit=(\S+)", command)
            if unit and unit.group(1) in self.units:
                return CommandResult(stdout="", stderr="Unit already exists", exit_code=1)
            if unit:
                self.units.add(unit.group(1))
        if f"systemctl stop '{_GUARD}-*" in command:
            self.units.clear()
        if any(f in command for f in self.fail):
            return CommandResult(stdout="", stderr="boom", exit_code=1)
        if "import dango" in command:
            return CommandResult(stdout="1.0.0", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    def write_remote_file(self, *args: Any, **kwargs: Any) -> None:
        self.cmds.append("write_remote_file")

    def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None


def _upgrade(ssh: Any, **kwargs: Any) -> Any:
    from dango.platform.cloud.upgrade import upgrade_dango

    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"
    with patch(_PATCH_BACKUP, return_value=backup):
        return upgrade_dango(ssh, version="1.2.0", **kwargs)


def _idx(cmds: list[str], pred: Any, *, start: int = 0) -> int:
    return next(i for i, c in enumerate(cmds) if i >= start and pred(c))


def _is_disarm(c: str) -> bool:
    return f"systemctl stop '{_GUARD}-*.timer'" in c


def _is_arm(c: str) -> bool:
    return c.startswith("systemd-run") and f"--unit={_GUARD}-" in c


def _arm_units(cmds: list[str]) -> list[str]:
    return [m.group(1) for c in cmds if _is_arm(c) if (m := re.search(r"--unit=(\S+)", c))]


@pytest.mark.unit
class TestUpgradeGuard:
    def test_upgrade_arms_and_disarms_guard(self) -> None:
        ssh = _RecordingSSH()
        _upgrade(ssh)
        c = ssh.cmds
        first_disarm = _idx(c, _is_disarm)
        arm = _idx(c, _is_arm)
        stop = _idx(c, lambda x: x == "systemctl stop dango-web")
        pip = _idx(c, lambda x: _PIP in x and not x.startswith("systemd-run"))
        start = _idx(c, lambda x: x == "systemctl start dango-web", start=pip)
        last_disarm = max(i for i, x in enumerate(c) if _is_disarm(x))
        assert first_disarm < arm < stop < pip < start < last_disarm
        assert f"reset-failed '{_GUARD}-*'" in c[first_disarm]

    def test_final_disarm_runs_when_pip_install_fails(self) -> None:
        ssh = _RecordingSSH(fail=(_PIP,))
        with pytest.raises(CloudProvisioningError):
            _upgrade(ssh)
        c = ssh.cmds
        start = _idx(c, lambda x: x == "systemctl start dango-web")
        assert any(_is_disarm(x) for x in c[start:])

    def test_upgrade_arms_after_previous_guard_fired(self) -> None:
        old = f"{_GUARD}-20200101T000000Z"
        ssh = _RecordingSSH(existing_units=(old,))
        result = _upgrade(ssh)
        units = _arm_units(ssh.cmds)
        assert units
        assert old not in units
        assert len(set(units)) == len(units)
        assert not [w for w in result.warnings if "guard" in w.lower()]

    def test_upgrade_rearms_guard_after_each_step(self) -> None:
        ssh = _RecordingSSH()
        _upgrade(ssh)
        c = ssh.cmds
        units = _arm_units(c)
        assert len(units) == 6 and len(set(units)) == 6  # arm, 2 stops, pip, migrations, rebuild
        arms = [i for i, x in enumerate(c) if _is_arm(x)]
        pip = _idx(c, lambda x: _PIP in x and not x.startswith("systemd-run"))
        migrate = _idx(c, lambda x: "apply_all_pending" in x and not x.startswith("systemd-run"))
        docker = _idx(c, lambda x: "up -d --build" in x and not x.startswith("systemd-run"))
        assert arms[0] < arms[1] < arms[2] < pip < arms[3] < migrate < arms[4] < docker < arms[5]
        for i, a in enumerate(arms[1:], start=1):
            stops = [x for x in c[a + 1 :] if f"stop '{units[i - 1]}.timer'" in x]
            assert stops, f"previous timer {units[i - 1]} not stopped after re-arm {i}"

    def test_upgrade_continues_when_guard_cannot_be_armed(self) -> None:
        ssh = _RecordingSSH(fail=("systemd-run",))
        result = _upgrade(ssh)
        assert result.new_version == "1.0.0"
        assert result.health_check_passed is True
        assert any("guard" in w.lower() for w in result.warnings)

    def test_venv_rebuild_runs_under_the_guard(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh = _RecordingSSH()

        def _rebuild(*a: Any, **k: Any) -> str:
            ssh.cmds.append("REBUILD")
            return "rebuilt"

        with (
            patch(
                "dango.platform.cloud.runtime_ownership.venv_is_untrusted",
                side_effect=itertools.chain([True], itertools.repeat(False)),
            ),
            patch(
                "dango.platform.cloud.runtime_ownership.trusted_metadata_version",
                return_value="1.2.0",
            ),
            patch(
                "dango.platform.cloud.runtime_ownership.rebuild_untrusted_venv",
                side_effect=_rebuild,
            ),
        ):
            upgrade_dango(ssh, version="1.2.0")
        c = ssh.cmds
        rebuild = c.index("REBUILD")
        assert any(_is_arm(x) for x in c[:rebuild])
        assert any(_is_disarm(x) for x in c[rebuild:])

    def test_guard_command_waits_while_upgrade_running(self, tmp_path: Path) -> None:
        ssh = _RecordingSSH()
        _upgrade(ssh)
        arm = next(x for x in ssh.cmds if _is_arm(x))
        argv = shlex.split(arm)
        assert "--collect" in argv and "--on-active=20m" in argv
        script = argv[-1]
        assert (
            "-lt 120" in script and "sleep 15" in script and "systemctl start dango-web" in script
        )

        patterns = re.findall(r'pgrep -f "([^"]+)"', script)
        assert len(patterns) >= 2
        real = [
            x
            for x in ssh.cmds
            if not x.startswith("systemd-run") and (_PIP in x or "up -d --build" in x)
        ]
        migrate = [
            x for x in ssh.cmds if "apply_all_pending" in x and not x.startswith("systemd-run")
        ]
        for cmd in [*real, *migrate]:
            assert any(re.search(p, cmd) for p in patterns), f"guard does not wait for: {cmd}"
        for p in patterns:  # the guard must not match its own sh -c command line
            assert not re.search(p, script)
            assert not re.search(p, arm)

        marker = tmp_path / "started"
        fast = script.replace("sleep 15", "sleep 0.2").replace(
            "systemctl start dango-web", f"touch {shlex.quote(str(marker))}"
        )

        def _worker(seconds: float) -> subprocess.Popen[bytes]:
            return subprocess.Popen(["sh", "-c", f"sleep {seconds}; : {_PIP} getdango"])

        # (a) waits while a matching process runs, starts the service after it ends
        worker = _worker(2.5)
        guard = subprocess.Popen(["sh", "-c", fast])
        try:
            time.sleep(1.0)
            assert worker.poll() is None and not marker.exists()
            guard.wait(timeout=15)
            assert worker.poll() is not None and marker.exists()
        finally:
            worker.kill()
            guard.kill()

        # (b) gives up waiting after the bound and starts the service anyway
        marker.unlink()
        worker = _worker(8)
        bounded = fast.replace("-lt 120", "-lt 3")
        guard = subprocess.Popen(["sh", "-c", bounded])
        try:
            guard.wait(timeout=6)
            assert worker.poll() is None and marker.exists()
        finally:
            worker.kill()
            guard.kill()
            if os.name == "posix":
                worker.wait()


def _droplet_client(status: str = "off") -> MagicMock:
    client = MagicMock()
    client.get_droplet.return_value = {"id": 42, "status": status}
    client.power_on.return_value = {"id": 7}
    client.wait_for_action.return_value = {"id": 7, "status": "completed"}
    return client


@pytest.mark.unit
class TestResizeRecovery:
    def test_resize_power_on_failure_is_reported_with_manual_command(self) -> None:
        from dango.platform.cloud.resize import resize_droplet

        client = _droplet_client("active")
        client.get_droplet.return_value = {
            "id": 42,
            "size_slug": "s-2vcpu-4gb",
            "networks": {"v4": [{"ip_address": "1.2.3.4", "type": "public"}]},
        }
        client.power_off.return_value = {"id": 1}
        client.resize.return_value = {"id": 2}
        client.wait_for_action.side_effect = [{"status": "completed"}, CloudError("resize failed")]
        client.power_on.side_effect = RuntimeError("api down")
        client.get_droplet.return_value = {**client.get_droplet.return_value, "status": "off"}
        backup = MagicMock()
        backup.archive_path = "/b.tar.gz"
        with (
            patch(_PATCH_BACKUP, return_value=backup),
            patch("dango.platform.cloud.resize.logger") as log,
            pytest.raises(CloudProvisioningError) as exc,
        ):
            resize_droplet(client, _RecordingSSH(), 42, "s-4vcpu-8gb")
        assert "resize failed" in str(exc.value)
        assert "dango remote power-on" in str(exc.value) and "42" in str(exc.value)
        assert log.error.call_args.args[0] == "resize_power_on_failed"

    def test_regenerate_profiles_reads_and_writes_as_dango(self) -> None:
        from dango.platform.cloud.resize import _PROFILES_PATH, regenerate_dbt_profiles

        ssh: Any = _RecordingSSH()
        original = ssh.exec_command

        def _exec(command: str, timeout: int | None = None) -> CommandResult:
            ssh.cmds.append(command)
            if "dbt_project.yml" in command:
                return CommandResult(stdout="name: proj\n", stderr="", exit_code=0)
            return original(command, timeout)

        ssh.exec_command = _exec
        written: list[str] = []
        ssh.write_remote_file = lambda path, *a, **k: written.append(path)
        assert regenerate_dbt_profiles(ssh, "s-4vcpu-8gb") is True
        assert written and all(p.startswith("/root/") for p in written)
        assert not any(p == _PROFILES_PATH for p in written)
        assert any("runuser -u dango" in c and _PROFILES_PATH in c for c in ssh.cmds)
        assert not any(c.startswith("cat ") for c in ssh.cmds)


@pytest.mark.unit
class TestPowerState:
    def test_get_power_state_and_power_on_and_wait(self) -> None:
        from dango.platform.cloud.resize import get_power_state, power_on_and_wait

        client = _droplet_client("OFF")
        assert get_power_state(client, 42) == "off"
        client.get_droplet.side_effect = CloudError("api")
        with pytest.raises(CloudProvisioningError):
            get_power_state(client, 42)

        client = _droplet_client("off")
        steps: list[str] = []
        with (
            patch(_PATCH_WAIT_DROPLET, side_effect=lambda *a, **k: steps.append("ready")),
            patch(_PATCH_WAIT_SSH, side_effect=lambda *a, **k: steps.append("ssh")) as wait_ssh,
        ):
            power_on_and_wait(client, 42, "1.2.3.4", on_progress=lambda s, st: steps.append(s))
        client.power_on.assert_called_once_with(42)
        client.wait_for_action.assert_called_once_with(7)
        assert wait_ssh.call_args.args[0] == "1.2.3.4"
        assert steps.index("ready") < steps.index("ssh")


def _cfg(provider: str = "digitalocean") -> MagicMock:
    cfg = MagicMock()
    cfg.droplet_id = 42
    cfg.droplet_ip = "1.2.3.4"
    cfg.size = "s-2vcpu-4gb"
    cfg.region = "nyc1"
    cfg.domain = None
    cfg.firewall_id = "fw"
    cfg.provider = provider
    cfg.ssh_key_path = ".dango/cloud_key"
    cfg.dbt_overrides = None
    return cfg


def _invoke(args: list[str], tmp_path: Path, *, client: Any, ssh: Any, extra: Any = ()) -> Any:
    from dango.cli.commands.remote import remote

    loader = MagicMock()
    loader.load_cloud_config.return_value = _cfg()
    make_client = (
        patch(_PATCH_MAKE_CLIENT, side_effect=client)
        if isinstance(client, Exception)
        else patch(_PATCH_MAKE_CLIENT, return_value=client)
    )
    with (
        patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
        patch(_PATCH_LOADER, return_value=loader),
        patch(_PATCH_SSH_CLS, return_value=ssh),
        make_client,
    ):
        return CliRunner().invoke(
            remote, args, obj={"project_root": tmp_path}, catch_exceptions=False
        )


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[^m]*m", "", text)


@pytest.mark.unit
class TestPowerOnCommand:
    def test_power_on_command_noop_when_active(self, tmp_path: Path) -> None:
        client = _droplet_client("active")
        ssh = MagicMock()
        with patch("dango.platform.cloud.resize.power_on_and_wait") as wait:
            result = _invoke(["power-on", "--yes"], tmp_path, client=client, ssh=ssh)
        assert result.exit_code == 0, result.output
        assert "already" in _plain(result.output).lower()
        wait.assert_not_called()

    def test_power_on_command_powers_on_when_off(self, tmp_path: Path) -> None:
        client = _droplet_client("off")
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        with (
            patch("dango.platform.cloud.resize.power_on_and_wait") as wait,
            patch("dango.platform.cloud.backup.start_services") as start,
            patch("dango.platform.cloud.backup.verify_health", return_value=True) as health,
        ):
            result = _invoke(["power-on", "--yes"], tmp_path, client=client, ssh=ssh)
        assert result.exit_code == 0, result.output
        wait.assert_called_once()
        assert wait.call_args.args[:3] == (client, 42, "1.2.3.4")
        start.assert_called_once()
        health.assert_called_once()
        assert "healthy" in _plain(result.output).lower()

    def test_resize_refuses_when_powered_off_with_hint(self, tmp_path: Path) -> None:
        ssh = MagicMock()
        with patch("dango.platform.cloud.resize.resize_droplet") as resize:
            result = _invoke(
                ["resize", "s-4vcpu-8gb", "--yes"], tmp_path, client=_droplet_client("off"), ssh=ssh
            )
        assert result.exit_code != 0
        assert "powered off" in _plain(result.output)
        assert "dango remote power-on" in _plain(result.output)
        resize.assert_not_called()
        ssh.connect.assert_not_called()

    def test_status_reports_powered_off_distinctly(self, tmp_path: Path) -> None:
        ssh = MagicMock()
        ssh.connect.side_effect = TimeoutError("SSH connection timed out")
        result = _invoke(["status"], tmp_path, client=_droplet_client("off"), ssh=ssh)
        out = _plain(result.output)
        assert result.exit_code == 1
        assert "Server is powered off" in out and "dango remote power-on" in out

    def test_status_without_token_hints_power_on(self, tmp_path: Path) -> None:
        ssh = MagicMock()
        ssh.connect.side_effect = TimeoutError("SSH connection timed out")
        result = _invoke(["status"], tmp_path, client=CloudAuthError("no token"), ssh=ssh)
        out = _plain(result.output)
        assert result.exit_code == 1
        assert "Failed to connect to server" in out
        assert "If the server is powered off: dango remote power-on" in out
