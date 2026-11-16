"""tests/unit/test_server_python_upgrade.py

remote upgrade and server setup ensure Python 3.11+ for the venv before anything is changed.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult
from tests.unit.test_runtime_ownership import _recording_ssh

_PINNED = "/opt/dango/python/cpython-3.12.16-20261026/bin/python3.12"
_HOST_MARK = 'command -v "$n"'
_INSTALL_MARK = "curl --proto"
_CHECK_MARK = ".dango-python.sha256"
_VENV_EXEC = re.compile(r"/srv/dango/venv/bin/(python3?|pip|dango)\b")


def _server(
    *,
    installed: str = "1.0.13",
    untrusted: bool = True,
    venv_python: str = "3.10",
    host: str = "",
    install_code: int = 0,
    install_error: Exception | None = None,
    extra: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[MagicMock, list[str]]:
    """A recording fake server (venv at *installed*, Ubuntu 22.04 style: no host Python)."""
    dist = f"/srv/dango/venv/lib/python{venv_python}/site-packages/getdango-{installed}.dist-info"
    results = {
        **(extra or {}),
        "getdango-*.dist-info": (dist + "\n", "", 0),
        "import dango": (installed, "", 0),
        "curl -sf": ("ok", "", 0),
    }
    if not untrusted:
        results["-print -quit"] = ("DANGO_PROBE_OK\n", "", 0)
    ssh, commands = _recording_ssh(results, untrusted_until_rebuild=untrusted)
    inner = ssh.exec_command.side_effect
    state = {"installed": False}

    def _exec(command: str, *args: Any, **kwargs: Any) -> CommandResult:
        if _HOST_MARK in command:
            commands.append(command)
            return CommandResult(stdout=f"{host}\n" if host else "", stderr="", exit_code=0)
        if command == "uname -m":
            commands.append(command)
            return CommandResult(stdout="x86_64\n", stderr="", exit_code=0)
        if _INSTALL_MARK in command:
            commands.append(command)
            if install_error is not None:
                raise install_error
            state["installed"] = install_code == 0
            return CommandResult(stdout="", stderr="download failed", exit_code=install_code)
        if _CHECK_MARK in command:
            commands.append(command)
            ok = state["installed"]
            return CommandResult(
                stdout="3.12.16.final\n" if ok else "", stderr="", exit_code=0 if ok else 1
            )
        if "pyvenv.cfg" in command:
            commands.append(command)
            return CommandResult(stdout=f"{venv_python}\n", stderr="", exit_code=0)
        return inner(command, *args, **kwargs)

    ssh.exec_command.side_effect = _exec
    return ssh, commands


def _upgrade(ssh: MagicMock, commands: list[str], *, latest: str = "1.0.14", **kwargs: Any) -> Any:
    from dango.platform.cloud.upgrade import upgrade_dango

    def _backup(*_a: Any, **_k: Any) -> Any:
        commands.append("BACKUP")
        return MagicMock(archive_path="/srv/dango/backups/deploy/backup-x.tar.gz")

    with (
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=latest),
        patch("dango.platform.cloud.backup.create_backup", side_effect=_backup),
    ):
        return upgrade_dango(ssh, **kwargs)


def _first(commands: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(commands) if needle in c)


def _changes(commands: list[str]) -> list[str]:
    """Commands that stop, arm, back up, rebuild or install anything."""
    pattern = r"systemctl stop|systemd-run|BACKUP|-m venv|mv /srv/dango/venv|pip install"
    return [c for c in commands if re.search(pattern, c)]


@pytest.mark.unit
class TestUpgradeOrder:
    def test_python_is_ensured_before_guard_backup_stop_and_rebuild(self) -> None:
        ssh, commands = _server()
        result = _upgrade(ssh, commands)
        ensured = _first(commands, _HOST_MARK)
        installed = _first(commands, _INSTALL_MARK)
        for later in ("systemd-run", "systemctl stop dango-web", "mv /srv/dango/venv ", "BACKUP"):
            assert ensured < installed < _first(commands, later), later
        assert result.new_version is not None or result.venv_rebuild

    def test_rebuild_uses_the_ensured_interpreter(self) -> None:
        ssh, commands = _server()
        result = _upgrade(ssh, commands)
        assert f"{_PINNED} -I -m venv /srv/dango/venv" in commands
        assert not any(c.startswith("python3 ") and "-m venv" in c for c in commands)
        assert "rebuilt" in (result.venv_rebuild or "")
        assert any("Installed Python 3.12.16" in w for w in result.warnings)

    def test_host_interpreter_is_used_without_a_download(self) -> None:
        ssh, commands = _server(host="/usr/bin/python3.12")
        _upgrade(ssh, commands)
        assert "/usr/bin/python3.12 -I -m venv /srv/dango/venv" in commands
        assert not any(_INSTALL_MARK in c for c in commands)

    @pytest.mark.parametrize("untrusted", [True, False])
    def test_failure_refuses_with_nothing_stopped_or_changed(self, untrusted: bool) -> None:
        ssh, commands = _server(untrusted=untrusted, install_code=10)
        with pytest.raises(CloudError) as excinfo:
            _upgrade(ssh, commands)
        message = str(excinfo.value)
        assert excinfo.value.error_code == "DANGO-D024"
        for phrase in (
            "Python 3.11, 3.12 or 3.13",
            "github.com",
            "Services were not stopped and Dango was not changed",
            "keeps running",
        ):
            assert phrase in message, phrase
        assert "before any change" not in message  # /opt/dango may have been created
        assert _changes(commands) == [], _changes(commands)
        if untrusted:  # an untrusted venv is never run by root
            assert [c for c in commands if _VENV_EXEC.search(c)] == []
        else:
            assert "Dango 1.0.13 keeps running" in message

    def test_ssh_failure_while_ensuring_python_is_a_python_refusal(self) -> None:
        from dango.exceptions import CloudSSHError

        ssh, commands = _server(install_error=CloudSSHError("SSH connection failed: timed out"))
        with pytest.raises(CloudError) as excinfo:
            _upgrade(ssh, commands)
        message = str(excinfo.value)
        assert excinfo.value.error_code == "DANGO-D024"
        for phrase in ("Python 3.11, 3.12 or 3.13", "timed out", "Services were not stopped"):
            assert phrase in message, phrase
        assert _changes(commands) == [], _changes(commands)

    def test_failed_build_still_restores_the_old_venv(self) -> None:
        ssh, commands = _server(extra={"-m venv /srv/dango/venv": ("", "no space left", 1)})
        with pytest.raises(CloudError, match="upgrade aborted"):
            _upgrade(ssh, commands)
        assert any(
            re.search(r"mv /srv/dango/venv\.untrusted-\S+ /srv/dango/venv", c) for c in commands
        )
        assert any("systemctl start dango-web" in c for c in commands)

    def test_trusted_venv_on_python_3_10_is_rebuilt(self) -> None:
        ssh, commands = _server(untrusted=False, venv_python="3.10")
        result = _upgrade(ssh, commands)
        assert any(
            re.search(r"mv /srv/dango/venv /srv/dango/venv\.untrusted-\S+", c) for c in commands
        )
        assert f"{_PINNED} -I -m venv /srv/dango/venv" in commands
        assert _first(commands, _HOST_MARK) < _first(commands, "systemctl stop dango-web")
        assert "rebuilt" in (result.venv_rebuild or "")

    def test_trusted_venv_in_range_is_upgraded_in_place(self) -> None:
        ssh, commands = _server(installed="1.0.14", untrusted=False, venv_python="3.12")
        result = _upgrade(ssh, commands, latest="1.0.15")
        assert not any(_HOST_MARK in c or "-m venv /srv/dango/venv" in c for c in commands)
        assert result.venv_rebuild is None

    def test_downgrade_below_1_0_14_needs_no_new_python(self) -> None:
        ssh, commands = _server(installed="1.0.14", untrusted=True, venv_python="3.12")
        _upgrade(ssh, commands, version="1.0.13", force=True)
        assert not any(_HOST_MARK in c or "pyvenv.cfg" in c for c in commands)
        assert "python3 -I -m venv /srv/dango/venv" in commands

    def test_at_target_with_a_trusted_venv_checks_nothing(self) -> None:
        ssh, commands = _server(installed="1.0.14", untrusted=False, venv_python="3.12")
        _upgrade(ssh, commands, latest="1.0.14")
        assert not any(_HOST_MARK in c or _INSTALL_MARK in c for c in commands)


@pytest.mark.unit
class TestSetup:
    def test_python_venv_step_builds_with_the_ensured_interpreter(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh, commands = _server(extra={"test -x /srv/dango/venv/bin/dango": ("", "", 1)})
        result = SetupResult()
        _setup_venv(ssh, result, None, dango_version="1.0.14")
        venv = next(c for c in commands if "-m venv /srv/dango/venv" in c)
        assert venv.startswith(f"{_PINNED} -I -m venv /srv/dango/venv && ")
        assert "getdango==1.0.14" in venv
        assert _first(commands, _INSTALL_MARK) < commands.index(venv)
        assert "python_venv" in result.steps_completed

    def test_python_venv_step_refuses_without_python(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh, commands = _server(
            install_code=12, extra={"test -x /srv/dango/venv/bin/dango": ("", "", 1)}
        )
        with pytest.raises(CloudProvisioningError, match="SHA-256") as excinfo:
            _setup_venv(ssh, SetupResult(), None)
        assert excinfo.value.error_code == "DANGO-D024"
        assert not any("-m venv" in c or "pip install" in c for c in commands)

    def test_existing_venv_needs_no_interpreter(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh, commands = _server(extra={"test -x /srv/dango/venv/bin/dango": ("", "", 0)})
        result = SetupResult()
        _setup_venv(ssh, result, None)
        assert not any(_HOST_MARK in c for c in commands)
        assert "python_venv" not in result.steps_completed  # left as it is

    def test_setup_rerun_rebuilds_an_untrusted_venv_with_the_ensured_interpreter(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_runtime_ownership

        ssh, commands = _server()
        _setup_runtime_ownership(ssh, SetupResult(), None)
        assert f"{_PINNED} -I -m venv /srv/dango/venv" in commands
        assert _first(commands, _HOST_MARK) < _first(commands, "systemctl stop dango-web")

    def test_setup_rerun_without_python_changes_nothing(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_runtime_ownership

        ssh, commands = _server(install_code=10)
        with pytest.raises(CloudProvisioningError, match="Python 3.11"):
            _setup_runtime_ownership(ssh, SetupResult(), None)
        assert _changes(commands) == [], _changes(commands)
