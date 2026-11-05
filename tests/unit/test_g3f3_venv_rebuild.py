"""tests/unit/test_g3f3_venv_rebuild.py

A slow or failed spaCy download is a warning, not an abort; leftover moved-aside venvs are removed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudSSHError
from dango.platform.cloud.ssh import CommandResult
from tests.factories.cloud_factories import host_python_server
from tests.unit.test_runtime_ownership import _recording_ssh

_RETRY = "/srv/dango/venv/bin/python -I -m spacy download en_core_web_sm"


def _spacy_fails(ssh: MagicMock, failure: Callable[[], CommandResult]) -> None:
    """Make every spaCy download on *ssh* fail with *failure* (a result, or raise inside it)."""
    inner = ssh.exec_command.side_effect

    def _exec(command: str, *args: Any, **kwargs: Any) -> CommandResult:
        if "spacy download" in command:
            return failure()
        return inner(command, *args, **kwargs)

    ssh.exec_command.side_effect = _exec


def _timeout() -> CommandResult:
    raise CloudSSHError("SSH connection failed: timed out")


def _exit_one() -> CommandResult:
    return CommandResult(stdout="", stderr="no network", exit_code=1)


@pytest.mark.unit
@pytest.mark.parametrize("failure", [_timeout, _exit_one], ids=["timeout", "exit-1"])
class TestSpacyIsOptional:
    def test_rebuild_continues_and_warns_with_the_retry_command(
        self, failure: Callable[[], CommandResult]
    ) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh(untrusted_until_rebuild=True)
        _spacy_fails(ssh, failure)
        warnings: list[str] = []
        status = ro.rebuild_untrusted_venv(ssh, warnings, version="1.0.15")
        assert status and status.startswith("venv rebuilt as root")
        assert any("en_core_web_sm" in w and _RETRY in w for w in warnings), warnings
        assert any(re.fullmatch(r"rm -rf /srv/dango/venv\.untrusted-\w+", c) for c in commands)

    def test_fresh_setup_continues_and_warns_with_the_retry_command(
        self, failure: Callable[[], CommandResult]
    ) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        ssh, _commands = _recording_ssh({"test -x /srv/dango/venv/bin/dango": ("", "", 1)})
        _spacy_fails(ssh, failure)
        result = SetupResult()
        with host_python_server():
            _setup_venv(ssh, result, None)
        assert "python_venv" in result.steps_completed
        assert any("en_core_web_sm" in w and _RETRY in w for w in result.warnings), result.warnings


_STALE_FIND = re.compile(r"find /srv/dango -maxdepth 1 .*-name 'venv\.untrusted-\*'.*-exec rm")


def _stale_commands(commands: list[str]) -> list[str]:
    return [c for c in commands if _STALE_FIND.search(c)]


def _idle() -> dict[str, tuple[str, str, int]]:
    """Answers that say: no other upgrade guard is active and no venv build is running."""
    from dango.platform.cloud.upgrade_guard import GUARD_ACTIVE_CMD
    from dango.platform.cloud.venv_leftovers import BUILD_RUNNING_CMD

    return {GUARD_ACTIVE_CMD: ("", "", 1), BUILD_RUNNING_CMD: ("", "", 1)}


@pytest.mark.unit
class TestStaleAsideVenvs:
    def test_sweep_removes_only_root_owned_real_directories(self) -> None:
        from dango.platform.cloud.venv_leftovers import remove_stale_venvs

        ssh, commands = _recording_ssh(_idle())
        remove_stale_venvs(ssh, [])
        cleanup = _stale_commands(commands)
        assert len(cleanup) == 1, commands
        assert "-type d" in cleanup[0] and "-uid 0" in cleanup[0]
        assert "rm -rf --one-file-system --" in cleanup[0]
        assert "-L" not in cleanup[0].split()
        assert '[ -x "$V/bin/dango" ] &&' in cleanup[0]

    def test_removal_is_reported_in_a_warning(self) -> None:
        from dango.platform.cloud.venv_leftovers import remove_stale_venvs

        removed = "/srv/dango/venv.untrusted-0123456789ab\n"
        ssh, _commands = _recording_ssh({**_idle(), "-exec rm": (removed, "", 0)})
        warnings: list[str] = []
        remove_stale_venvs(ssh, warnings)
        assert any("venv.untrusted-0123456789ab" in w and "removed" in w.lower() for w in warnings)

    def test_nothing_removed_means_no_warning(self) -> None:
        from dango.platform.cloud.venv_leftovers import remove_stale_venvs

        ssh, _commands = _recording_ssh(_idle())
        warnings: list[str] = []
        remove_stale_venvs(ssh, warnings)
        assert not any("untrusted-" in w for w in warnings)

    def test_leftovers_that_are_kept_are_named_in_a_warning(self) -> None:
        from dango.platform.cloud.venv_leftovers import remove_stale_venvs

        kept = "/srv/dango/venv.untrusted-owned\n/srv/dango/venv.untrusted-link\n"
        ssh, _commands = _recording_ssh(
            {**_idle(), "-exec rm": ("", "", 0), "-name 'venv.untrusted-*'": (kept, "", 0)}
        )
        warnings: list[str] = []
        remove_stale_venvs(ssh, warnings)
        text = " ".join(warnings)
        assert "venv.untrusted-owned" in text and "venv.untrusted-link" in text
        assert "not removed" in text.lower() or "left in place" in text.lower()

    def test_rebuild_itself_does_not_sweep(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh(_idle(), untrusted_until_rebuild=True)
        ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")
        assert _stale_commands(commands) == [], commands


@pytest.mark.unit
class TestSweepRunsOnlyWhenNoUpgradeIsRunning:
    """Run B's sweep must never delete the venv a live run A moved aside to restore from."""

    def test_idle_server_upgrade_sweeps_before_it_arms_a_guard_or_moves_the_venv(self) -> None:
        from tests.unit.test_runtime_ownership import _run_upgrade

        _ssh, commands, _result = _run_upgrade(
            "1.0.14", "1.0.15", venv_untrusted=True, extra=_idle()
        )
        sweep = _stale_commands(commands)
        assert len(sweep) == 1, commands
        at = commands.index(sweep[0])
        assert not any("systemd-run" in c for c in commands[:at]), commands[:at]
        assert not any(c.startswith("mv /srv/dango/venv ") for c in commands[:at])

    def test_trusted_upgrade_also_sweeps(self) -> None:
        from tests.unit.test_runtime_ownership import _run_upgrade

        _ssh, commands, _result = _run_upgrade("1.0.14", "1.0.15", extra=_idle())
        assert len(_stale_commands(commands)) == 1, commands

    def test_active_guard_of_another_run_blocks_the_sweep(self) -> None:
        from dango.platform.cloud.upgrade_guard import GUARD_ACTIVE_CMD
        from tests.unit.test_runtime_ownership import _run_upgrade

        extra = {**_idle(), GUARD_ACTIVE_CMD: ("", "", 0)}
        _ssh, commands, _result = _run_upgrade("1.0.14", "1.0.15", extra=extra)
        assert _stale_commands(commands) == [], commands

    def test_running_venv_build_blocks_the_sweep(self) -> None:
        from dango.platform.cloud.venv_leftovers import BUILD_RUNNING_CMD
        from tests.unit.test_runtime_ownership import _run_upgrade

        extra = {**_idle(), BUILD_RUNNING_CMD: ("", "", 0)}
        _ssh, commands, _result = _run_upgrade("1.0.14", "1.0.15", extra=extra)
        assert _stale_commands(commands) == [], commands

    def test_an_unreadable_gate_means_no_sweep(self) -> None:
        from dango.exceptions import CloudSSHError
        from dango.platform.cloud.upgrade_guard import GUARD_ACTIVE_CMD
        from dango.platform.cloud.venv_leftovers import remove_stale_venvs

        ssh, commands = _recording_ssh(_idle())
        inner = ssh.exec_command.side_effect

        def _exec(command: str, *args: Any, **kwargs: Any) -> CommandResult:
            if command == GUARD_ACTIVE_CMD:
                raise CloudSSHError("connection lost")
            return inner(command, *args, **kwargs)

        ssh.exec_command.side_effect = _exec
        remove_stale_venvs(ssh, [])
        assert _stale_commands(commands) == []


@pytest.mark.unit
class TestSpacyDiesWithTheSshCommand:
    def test_server_side_timeout_is_shorter_than_the_ssh_timeout(self) -> None:
        from dango.platform.cloud.venv_leftovers import install_spacy_model

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        install_spacy_model(ssh, [], timeout=180)
        command = ssh.exec_command.call_args.args[0]
        match = re.search(r"\btimeout (?:-k \d+ )?(\d+)s? ", command)
        assert match, command
        assert 0 < int(match.group(1)) < ssh.exec_command.call_args.kwargs["timeout"]
        assert "-m spacy download en_core_web_sm" in command

    def test_the_retry_command_in_the_warning_has_no_timeout_wrapper(self) -> None:
        from dango.platform.cloud.venv_leftovers import install_spacy_model

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=124)
        warnings: list[str] = []
        install_spacy_model(ssh, warnings, timeout=180)
        assert any(_RETRY in w and "timeout" not in w.split("manually:")[-1] for w in warnings)
