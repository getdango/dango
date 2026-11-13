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


def _listing(*names: str) -> dict[str, tuple[str, str, int]]:
    """The server's answer to the read-only leftover listing (NUL separated, full paths)."""
    return {"-print0": ("".join(f"/srv/dango/{n}\0" for n in names), "", 0)}


@pytest.mark.unit
class TestLeftoverVenvWarning:
    """Leftover moved-aside venvs are only reported; an operator removes them after checking."""

    def test_each_leftover_gets_the_exact_cleanup_command_without_a_glob(self) -> None:
        from dango.platform.cloud.venv_leftovers import warn_stale_venvs

        names = ("venv.untrusted-0123456789ab", "venv.untrusted-abcdefabcdef")
        ssh, _commands = _recording_ssh(_listing(*names))
        warnings: list[str] = []
        warn_stale_venvs(ssh, warnings)
        text = " ".join(warnings)
        for name in names:
            assert f"rm -rf /srv/dango/{name}" in text
        assert "*" not in text
        assert "root" in text and "no upgrade is running" in text

    def test_the_check_only_reads(self) -> None:
        from dango.platform.cloud.venv_leftovers import warn_stale_venvs

        ssh, commands = _recording_ssh(_listing("venv.untrusted-0123456789ab"))
        warn_stale_venvs(ssh, [])
        assert len(commands) == 1, commands
        assert "rm " not in commands[0] and "-exec" not in commands[0]
        assert "-delete" not in commands[0]

    def test_control_characters_and_markup_in_names_are_neutralised(self) -> None:
        from dango.platform.cloud.venv_leftovers import warn_stale_venvs

        evil = "venv.untrusted-\x1b[2J\x07[red]boom[/red]"
        ssh, _commands = _recording_ssh(_listing(evil))
        warnings: list[str] = []
        warn_stale_venvs(ssh, warnings)
        text = " ".join(warnings)
        assert "\x1b" not in text and "\x07" not in text
        assert "[red]" not in text.replace("\\[red]", "")
        assert "rm -rf" not in text  # an unexpected name gets no ready-made command

    def test_no_leftovers_means_no_warning(self) -> None:
        from dango.platform.cloud.venv_leftovers import warn_stale_venvs

        ssh, _commands = _recording_ssh({"-print0": ("", "", 0)})
        warnings: list[str] = []
        warn_stale_venvs(ssh, warnings)
        assert warnings == []

    def test_an_ssh_error_is_not_fatal(self) -> None:
        from dango.platform.cloud.venv_leftovers import warn_stale_venvs

        ssh = MagicMock()
        ssh.exec_command.side_effect = CloudSSHError("lost")
        warn_stale_venvs(ssh, [])

    def test_upgrade_warns_first_and_removes_nothing_itself(self) -> None:
        from tests.unit.test_runtime_ownership import _run_upgrade

        leftover = "venv.untrusted-0123456789ab"
        _ssh, commands, result = _run_upgrade(
            "1.0.14", "1.0.15", venv_untrusted=True, extra=_listing(leftover)
        )
        listing = next(i for i, c in enumerate(commands) if "-print0" in c)
        assert not any("systemd-run" in c for c in commands[:listing])
        assert not any(c.startswith("mv /srv/dango/venv ") for c in commands[:listing])
        assert not any(f"rm -rf /srv/dango/{leftover}" in c for c in commands)
        assert any(f"rm -rf /srv/dango/{leftover}" in w for w in result.warnings)


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
