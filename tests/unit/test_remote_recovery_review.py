"""tests/unit/test_remote_recovery_review.py

Review follow-ups for recovery from an interrupted `remote upgrade` / `remote resize`: guard
re-arm ordering, venv-rebuild coverage, resize cleanup behaviour, and size drift on power-on.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult
from dango.platform.cloud.upgrade_guard import GUARD_ACTION, UpgradeGuard

_GUARD = "dango-upgrade-guard"
_PATCH_WAIT_DROPLET = "dango.platform.cloud.provisioning.wait_for_droplet_ready"
_PATCH_WAIT_SSH = "dango.platform.cloud.provisioning.wait_for_ssh"
_PATCH_BACKUP = "dango.platform.cloud.backup.create_backup"


class _SSH:
    def __init__(self, *, fail: tuple[str, ...] = ()) -> None:
        self.cmds: list[str] = []
        self.fail = fail

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.cmds.append(command)
        code = 1 if any(f in command for f in self.fail) else 0
        return CommandResult(stdout="", stderr="", exit_code=code)

    def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None


def _is_arm(c: str) -> bool:
    return c.startswith("systemd-run")


@pytest.mark.unit
class TestGuardRearmOrder:
    def test_new_timer_is_armed_before_previous_is_stopped(self) -> None:
        ssh: Any = _SSH()
        guard = UpgradeGuard(ssh, [])
        guard.arm()
        guard.rearm()
        arms = [i for i, c in enumerate(ssh.cmds) if _is_arm(c)]
        first = re.search(r"--unit=(\S+)", ssh.cmds[arms[0]])
        assert first and len(arms) == 2
        stop = next(i for i, c in enumerate(ssh.cmds) if f"stop '{first.group(1)}.timer'" in c)
        assert arms[1] < stop

    def test_previous_timer_kept_when_new_one_cannot_be_armed(self) -> None:
        ssh: Any = _SSH()
        guard = UpgradeGuard(ssh, [])
        guard.arm()
        ssh.fail = ("systemd-run",)
        ssh.cmds.clear()
        guard.rearm()
        assert [c for c in ssh.cmds if "systemctl stop" in c] == []


_REBUILD_LINES = [
    "cd / && /srv/dango/venv/bin/python -I -m spacy download en_core_web_sm -q",
    "/srv/dango/venv/bin/python -m pip install --upgrade pip",
    "python3 -m venv /srv/dango/venv",
    "/bin/sh /usr/local/sbin/dango-compose up-build",
    "/bin/sh /usr/local/sbin/dango-compose build",
    "/bin/sh /usr/local/sbin/dango-compose pull",
    "docker compose --project-directory /srv/dango/compose -p dango-abcd1234 "
    "-f /srv/dango/compose/docker-compose.yml up -d --build metabase",
]
_NOT_REBUILD_LINES = [
    "/bin/sh /usr/local/sbin/dango-compose up",
    "/bin/sh /usr/local/sbin/dango-compose restart metabase",
    "/bin/sh /usr/local/sbin/dango-compose ps",
]


@pytest.mark.unit
class TestRebuildCoverage:
    def test_guard_patterns_match_rebuild_steps_but_not_the_guard(self) -> None:
        patterns = re.findall(r'pgrep -f "([^"]+)"', GUARD_ACTION)
        for line in _REBUILD_LINES:
            assert any(re.search(p, line) for p in patterns), line
        for p in patterns:
            assert not re.search(p, GUARD_ACTION)
            assert not re.search(p, shlex.join(["systemd-run", GUARD_ACTION]))

    def test_guard_ignores_helper_calls_that_do_not_rebuild(self) -> None:
        patterns = re.findall(r'pgrep -f "([^"]+)"', GUARD_ACTION)
        for line in _NOT_REBUILD_LINES:
            assert not any(re.search(p, line) for p in patterns), line

    def test_guard_waits_in_real_sh_while_spacy_download_runs(self, tmp_path: Path) -> None:
        marker = tmp_path / "started"
        script = GUARD_ACTION.replace("sleep 15", "sleep 0.2").replace(
            "systemctl start dango-web", f"touch {shlex.quote(str(marker))}"
        )
        argv = shlex.split(script)
        worker = subprocess.Popen(["sh", "-c", f"sleep 2.5; : {_REBUILD_LINES[0]}"])
        guard = subprocess.Popen(argv)
        try:
            time.sleep(1.0)
            assert worker.poll() is None and not marker.exists()
            guard.wait(timeout=15)
            assert marker.exists()
        finally:
            worker.kill()
            guard.kill()
            worker.wait()

    def test_rebuild_venv_reports_each_step(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ticks: list[int] = []
        ssh: Any = _SSH()
        with (
            patch.object(ro, "venv_trust_findings", return_value=[]),
            patch.object(ro, "install_pushed_requirements"),
        ):
            ro.rebuild_venv(
                ssh,
                [],
                version="1.2.0",
                local_project_root=Path("."),
                on_step=lambda: ticks.append(len(ssh.cmds)),
            )
        assert len(ticks) >= 6
        assert ticks == sorted(ticks)

    def test_upgrade_rearms_guard_on_every_rebuild_step(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh: Any = _SSH()
        seen: dict[str, Any] = {}

        def _rebuild(*a: Any, **k: Any) -> str:
            seen["cb"] = k.get("on_step")
            before = sum(_is_arm(c) for c in ssh.cmds)
            assert callable(seen["cb"])
            seen["cb"]()
            seen["delta"] = sum(_is_arm(c) for c in ssh.cmds) - before
            return "rebuilt"

        with (
            patch(
                "dango.platform.cloud.runtime_ownership.venv_is_untrusted",
                side_effect=[True, False, False, False],
            ),
            patch(
                "dango.platform.cloud.runtime_ownership.trusted_metadata_version",
                return_value="1.2.0",
            ),
            patch(
                "dango.platform.cloud.runtime_ownership.rebuild_untrusted_venv",
                side_effect=_rebuild,
            ),
            patch("dango.platform.cloud.runtime_ownership.secure_runtime", return_value="ok"),
            patch(_PATCH_BACKUP, return_value=MagicMock(archive_path="/b")),
            patch("dango.platform.cloud.backup.verify_health", return_value=True),
            patch("dango.platform.cloud.backup.start_services"),
            patch("dango.platform.cloud.backup.stop_services"),
        ):
            upgrade_dango(ssh, version="1.2.0")
        assert seen["delta"] == 1


def _client(state: str = "active") -> MagicMock:
    client = MagicMock()
    client.get_droplet.return_value = {
        "id": 42,
        "status": state,
        "size_slug": "s-2vcpu-4gb",
        "networks": {"v4": [{"ip_address": "1.2.3.4", "type": "public"}]},
    }
    client.power_off.return_value = {"id": 1}
    client.resize.return_value = {"id": 2}
    client.power_on.return_value = {"id": 3}
    client.wait_for_action.return_value = {"status": "completed"}
    return client


def _resize(client: Any, ssh: Any) -> Any:
    from dango.platform.cloud.resize import resize_droplet

    with patch(_PATCH_BACKUP, return_value=MagicMock(archive_path="/b")):
        return resize_droplet(client, ssh, 42, "s-4vcpu-8gb")


@pytest.mark.unit
class TestResizeCleanup:
    def _fail_resize(self, exc: BaseException, state: str) -> MagicMock:
        client = _client(state)
        client.wait_for_action.side_effect = [{"status": "completed"}, exc]
        client.power_on.side_effect = RuntimeError("already on")
        return client

    def test_hint_only_when_still_powered_off(self) -> None:
        client = self._fail_resize(CloudError("resize failed"), "off")
        with pytest.raises(CloudProvisioningError) as exc:
            _resize(client, _SSH())
        assert "dango remote power-on" in str(exc.value)

    def test_original_exception_reraised_when_not_powered_off(self) -> None:
        original = CloudError("resize failed")
        client = self._fail_resize(original, "active")
        with pytest.raises(CloudError) as exc:
            _resize(client, _SSH())
        assert exc.value is original

    def test_keyboard_interrupt_still_tries_power_on(self) -> None:
        client = _client("off")
        client.wait_for_action.side_effect = [{"status": "completed"}, KeyboardInterrupt()]
        with pytest.raises(KeyboardInterrupt):
            _resize(client, _SSH())
        client.power_on.assert_called_once()

    def test_keyboard_interrupt_while_off_prints_power_on_hint(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = _client("off")
        client.wait_for_action.side_effect = [{"status": "completed"}, KeyboardInterrupt()]
        client.power_on.side_effect = RuntimeError("api down")
        with pytest.raises(KeyboardInterrupt):
            _resize(client, _SSH())
        captured = capsys.readouterr()
        assert "dango remote power-on" in captured.out + captured.err

    def test_ssh_error_in_dbt_profiles_still_starts_services(self) -> None:
        client = _client("active")
        with (
            patch(_PATCH_WAIT_DROPLET),
            patch(_PATCH_WAIT_SSH),
            patch(
                "dango.platform.cloud.resize.regenerate_dbt_profiles",
                side_effect=OSError("ssh dropped"),
            ),
            patch("dango.platform.cloud.backup.start_services") as start,
            patch("dango.platform.cloud.backup.verify_health", return_value=True),
        ):
            result = _resize(client, _SSH())
        start.assert_called_once()
        assert any("profiles.yml was not regenerated" in w for w in result.warnings)


@pytest.mark.unit
class TestPowerOnSizeDrift:
    def _run(
        self,
        tmp_path: Path,
        live_size: str,
        *,
        state: str = "off",
        size_lookup_error: Exception | None = None,
    ) -> tuple[Any, MagicMock, MagicMock]:
        from dango.cli.commands.remote import remote

        cfg = MagicMock()
        cfg.droplet_id, cfg.droplet_ip, cfg.size = 42, "1.2.3.4", "s-2vcpu-4gb"
        cfg.region, cfg.provider, cfg.dbt_overrides = "nyc1", "digitalocean", None
        loader = MagicMock()
        loader.load_cloud_config.return_value = cfg
        client = MagicMock()
        client.get_droplet.side_effect = [
            {"status": state, "size_slug": live_size},
            size_lookup_error or {"status": "active", "size_slug": live_size},
        ]
        if state == "active":
            client.get_droplet.side_effect = [{"status": state, "size_slug": live_size}] * 2
        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.loader.ConfigLoader", return_value=loader),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
            patch("dango.cli.commands.remote._make_client", return_value=client),
            patch("dango.platform.cloud.resize.power_on_and_wait"),
            patch("dango.platform.cloud.backup.start_services"),
            patch("dango.platform.cloud.backup.verify_health", return_value=True),
            patch("dango.platform.cloud.resize.regenerate_dbt_profiles") as regen,
            patch("dango.platform.cloud.provisioning.save_provisioning_metadata") as save,
        ):
            result = CliRunner().invoke(
                remote, ["power-on", "--yes"], obj={"project_root": tmp_path}
            )
        return result, regen, save

    def test_size_changed_while_client_was_gone_is_recorded(self, tmp_path: Path) -> None:
        result, regen, save = self._run(tmp_path, "s-4vcpu-8gb")
        assert result.exit_code == 0, result.output
        regen.assert_called_once()
        assert regen.call_args.args[1] == "s-4vcpu-8gb"
        assert save.call_args.kwargs["size"] == "s-4vcpu-8gb"
        assert "s-4vcpu-8gb" in re.sub(r"\x1b\[[^m]*m", "", result.output)

    def test_unchanged_size_leaves_config_alone(self, tmp_path: Path) -> None:
        result, regen, save = self._run(tmp_path, "s-2vcpu-4gb")
        assert result.exit_code == 0, result.output
        regen.assert_not_called()
        save.assert_not_called()

    def test_size_reconciled_when_server_is_already_powered_on(self, tmp_path: Path) -> None:
        result, regen, save = self._run(tmp_path, "s-4vcpu-8gb", state="active")
        assert result.exit_code == 0, result.output
        regen.assert_called_once()
        assert save.call_args.kwargs["size"] == "s-4vcpu-8gb"

    def test_size_lookup_failure_is_a_warning_not_a_power_on_failure(self, tmp_path: Path) -> None:
        result, _, _ = self._run(
            tmp_path, "s-4vcpu-8gb", size_lookup_error=RuntimeError("api blip")
        )
        out = re.sub(r"\x1b\[[^m]*m", "", result.output)
        assert result.exit_code == 0, out
        assert "Power-on failed" not in out
        assert "size" in out.lower() and "api blip" in out
