"""tests/unit/test_validators_trailing_newline.py

Validators must reject a trailing newline (``$`` + ``re.match`` accepts it); the admin email reaches a remote shell only quoted.
"""

import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import click
import pytest

from dango.analysis.models import MonitorConfig
from dango.cli.commands.deploy_provision import (
    _create_admin_and_enable_auth,
    _trigger_metabase_setup,
)
from dango.cli.commands.source import _parse_duration
from dango.config.schedules import ScheduleConfig
from dango.exceptions import CloudError, CloudProvisioningError, InvalidSourceNameError
from dango.platform.cloud.deployer import _validate_model_names
from dango.platform.cloud.project_files import _APPEND_SCRIPT, append_env_line_if_missing
from dango.platform.cloud.resize import validate_size_slug
from dango.validation import parse_backfill_duration, validate_identifier, validate_source_name

BAD_SUFFIXES = ["\n", "\r\n"]


def _monitor(name: str) -> MonitorConfig:
    return MonitorConfig(name=name, source_table="raw_x.t", value_expression="count(*)")


def _schedule(name: str) -> ScheduleConfig:
    return ScheduleConfig(name=name, cron="0 2 * * *", sources=["s"])


# (callable, valid value, exception raised for a bad value)
NAME_VALIDATORS: list[tuple[Any, str, type[Exception]]] = [
    (validate_source_name, "abc", InvalidSourceNameError),
    (validate_identifier, "abc", InvalidSourceNameError),
    (parse_backfill_duration, "7d", ValueError),
    (_parse_duration, "7d", click.BadParameter),
    (validate_size_slug, "s-2vcpu-4gb", CloudError),
    (lambda v: _validate_model_names([v]), "abc", CloudProvisioningError),
    (_monitor, "abc", ValueError),
    (_schedule, "abc", ValueError),
]
IDS = [
    "source_name",
    "identifier",
    "backfill_duration",
    "cli_duration",
    "size_slug",
    "dbt_model_names",
    "monitor_name",
    "schedule_name",
]


@pytest.mark.unit
class TestNameValidatorsRejectTrailingNewline:
    @pytest.mark.parametrize(("validator", "valid", "exc"), NAME_VALIDATORS, ids=IDS)
    def test_valid_value_still_accepted(self, validator: Any, valid: str, exc: type) -> None:
        # does-not-raise: a clean value is accepted
        result = validator(valid)
        # validators return None or the parsed (truthy) value
        assert result is None or bool(result)

    @pytest.mark.parametrize(("validator", "valid", "exc"), NAME_VALIDATORS, ids=IDS)
    @pytest.mark.parametrize("suffix", BAD_SUFFIXES)
    def test_trailing_newline_rejected(
        self, validator: Any, valid: str, exc: type, suffix: str
    ) -> None:
        with pytest.raises(exc):
            validator(valid + suffix)

    @pytest.mark.parametrize(("validator", "valid", "exc"), NAME_VALIDATORS, ids=IDS)
    def test_leading_space_rejected(self, validator: Any, valid: str, exc: type) -> None:
        with pytest.raises(exc):
            validator(" " + valid)


def _ssh() -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = MagicMock(exit_code=0, stdout="", stderr="", success=True)
    return ssh


@pytest.mark.unit
class TestAdminEmailValidation:
    @pytest.mark.parametrize(
        "email",
        [
            "a@b.co\n",
            "a@b.co\r\n",
            " a@b.co",
            "a@b.co ",
            "a@b.co\nFOO=bar",
            "x'; touch /tmp/x; '@b.co",
        ],
    )
    @pytest.mark.parametrize("fn", [_create_admin_and_enable_auth, _trigger_metabase_setup])
    def test_bad_email_rejected_before_any_command(self, email: str, fn: Any) -> None:
        ssh = _ssh()
        args = (ssh, email, "pw") if fn is _create_admin_and_enable_auth else (ssh, email)
        with pytest.raises(ValueError, match="Invalid email"):
            fn(*args)
        ssh.exec_command.assert_not_called()

    def test_injection_string_rejected(self) -> None:
        ssh = _ssh()
        with pytest.raises(ValueError):
            _trigger_metabase_setup(ssh, "'; touch /tmp/x; '")
        ssh.exec_command.assert_not_called()

    def test_admin_email_command_is_quoted(self) -> None:
        ssh = _ssh()
        _trigger_metabase_setup(ssh, "first.last+tag@example.com")
        command = ssh.exec_command.call_args_list[0].args[0]
        q = shlex.quote
        assert command == (
            f"runuser -u dango -- sh -c {q(_APPEND_SCRIPT)} _ /srv/dango/project/.env "
            f"DANGO_ADMIN_EMAIL {q('DANGO_ADMIN_EMAIL=first.last+tag@example.com')}"
        )

    @pytest.mark.parametrize("value", ["a\nB=c", "a\rB=c", "a\0b"])
    def test_env_append_refuses_control_characters(self, value: str) -> None:
        ssh = _ssh()
        with pytest.raises(ValueError):
            append_env_line_if_missing(ssh, "/srv/dango/project/.env", "KEY", value)
        ssh.exec_command.assert_not_called()

    def test_env_append_quotes_shell_metacharacters(self) -> None:
        ssh = _ssh()
        append_env_line_if_missing(ssh, "/p/.env", "KEY", "it's $(id)")
        command = ssh.exec_command.call_args.args[0]
        assert shlex.quote("KEY=it's $(id)") in command


# Anchored ``^…$`` patterns that are not validators of user input: they match
# single lines of files or listings Dango reads, where a newline cannot occur.
_LINE_PARSERS = {
    "dango/migrations/runner.py",
    "dango/logging.py",
    "dango/cli/validate.py",
    "dango/platform/cloud/server_auth.py",
    "dango/platform/cloud/server_ports.py",
}
_ANCHORED = re.compile(r're\.compile\(\s*r?["\']\^.*\$["\']')


@pytest.mark.unit
def test_no_anchored_validator_regexes_left() -> None:
    root = Path(__file__).resolve().parents[2] / "dango"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root.parent).as_posix()
        if "dlt_sources" in rel or rel in _LINE_PARSERS:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _ANCHORED.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "use re.fullmatch without ^/$ anchors:\n" + "\n".join(offenders)
