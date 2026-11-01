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


_STALE_FIND = re.compile(r"find /srv/dango -maxdepth 1 .*-name 'venv\.untrusted-\*'")


def _stale_commands(commands: list[str]) -> list[str]:
    return [c for c in commands if _STALE_FIND.search(c)]


@pytest.mark.unit
class TestStaleAsideVenvs:
    def test_rebuild_removes_only_root_owned_real_directories_first(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh(untrusted_until_rebuild=True)
        ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")
        cleanup = _stale_commands(commands)
        assert len(cleanup) == 1, commands
        assert "-type d" in cleanup[0] and "-uid 0" in cleanup[0]
        assert "rm -rf --one-file-system --" in cleanup[0]
        assert "-L" not in cleanup[0].split()
        first_move = next(i for i, c in enumerate(commands) if c.startswith("mv /srv/dango/venv "))
        assert commands.index(cleanup[0]) < first_move

    def test_cleanup_only_runs_while_the_current_venv_exists(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh(untrusted_until_rebuild=True)
        ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")
        assert '[ -x "$V/bin/dango" ] &&' in _stale_commands(commands)[0]

    def test_removal_is_reported_in_a_warning(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        removed = "/srv/dango/venv.untrusted-0123456789ab\n"
        ssh, _commands = _recording_ssh(
            {"-name 'venv.untrusted-*'": (removed, "", 0)}, untrusted_until_rebuild=True
        )
        warnings: list[str] = []
        ro.rebuild_untrusted_venv(ssh, warnings, version="1.0.15")
        assert any("venv.untrusted-0123456789ab" in w and "removed" in w.lower() for w in warnings)

    def test_nothing_removed_means_no_warning(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, _commands = _recording_ssh(untrusted_until_rebuild=True)
        warnings: list[str] = []
        ro.rebuild_untrusted_venv(ssh, warnings, version="1.0.15")
        assert not any("untrusted-" in w for w in warnings)

    def test_upgrade_without_a_rebuild_also_removes_leftovers(self) -> None:
        from tests.unit.test_runtime_ownership import _run_upgrade

        _ssh, commands, _result = _run_upgrade("1.0.14", "1.0.15")
        assert len(_stale_commands(commands)) == 1, commands
