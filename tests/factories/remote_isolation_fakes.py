"""tests/factories/remote_isolation_fakes.py

A pretend root SSH session for ``dango remote isolation``: it keeps the two markers, the web
service, the guard and the audit calls, and recognises steps by their ``# runner-step:NAME`` tag.
Nothing here runs a shell.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner, Result

from dango.cli.commands.remote import remote
from dango.platform.cloud.ssh import CommandResult

GUARD_PREFIX = "dango-upgrade-guard"
IP = "203.0.113.1"
VALID_RECORD = '{"dango": "1.0.14", "runner_isolation": {"schema": 1, "loopback": "restricted"}}'
DROPPED_RECORD = '{"dango": "1.0.14", "docker_access": "helper"}'


def probe(
    active: str | None = "1",
    disabled: str | None = "0",
    nft: str | None = "1",
    users: str | None = "2",
    sudoers: str | None = "1",
    record: str = VALID_RECORD,
    disabled_info: str | None = None,
) -> str:
    lines = [
        f"{key}={value}"
        for key, value in (
            ("active", active),
            ("disabled", disabled),
            ("nft", nft),
            ("users", users),
            ("sudoers", sudoers),
            ("disabled_info", disabled_info),
        )
        if value is not None
    ]
    return "\n".join([*lines, "---", record]) + "\n"


class IsoSSH:
    """Pretend root session: keeps the markers, the web service and the audit calls."""

    def __init__(
        self,
        *,
        active: bool = True,
        disabled: bool = False,
        nft: bool = True,
        record: str = VALID_RECORD,
        docker_group: bool = False,
        fail: dict[str, int] | None = None,
        audit_rc: int = 0,
        web: bool = True,
        connect_error: Exception | None = None,
        build: bool = True,
        disabled_info: str | None = None,
        guard_active: bool = False,
        guard_units: list[str] | None = None,
        after_arm: list[str] | None = None,
        unit_ages: dict[str, int | None] | None = None,
        fail_detail: str = "last line",
    ) -> None:
        self.disabled_info = disabled_info
        self.fail_detail = fail_detail
        #: guard units listed by systemd; ``guard_active`` stands for one left by an upgrade
        self.units = list(guard_units or [])
        if guard_active:
            self.units.append(f"{GUARD_PREFIX}-20261025T000000Z.timer")
        #: units another command arms right after ours (the race window)
        self.after_arm = after_arm or []
        #: seconds since a guard unit was last armed (``None``: unreadable); default: long ago
        self.unit_ages = unit_ages or {}
        self.nonces: list[str] = []
        self.connect_error = connect_error
        self.build = build
        self.active, self.disabled, self.nft = active, disabled, nft
        self.record = record
        self.docker_group = docker_group
        self.fail = fail or {}
        self.audit_rc = audit_rc
        self.web = web
        self.commands: list[str] = []
        self.events: list[str] = []
        self.audits: list[tuple[str, str, str]] = []

    def connect(self, _ip: str, **_kw: Any) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def disconnect(self) -> None:
        return None

    def exec_command(self, command: str, timeout: int | None = None, **_kw: Any) -> CommandResult:
        self.commands.append(command)
        if "echo disabled=" in command and "echo active=" not in command:
            return CommandResult(f"disabled={1 if self.disabled else 0}\n", "", 0)
        if "echo active=" in command:
            return CommandResult(self._probe(), "", 0)
        if "compose/active" in command:
            groups = "dango docker" if self.docker_group else "dango"
            marker = "0" if self.docker_group else "1"
            return CommandResult(f"marker={marker}\ngroups={groups}\n---\n{self.record}\n", "", 0)
        if "log_auth_event" in command:
            tokens = shlex.split(command)
            event, actor, server, nonce = tokens[tokens.index("-c") + 2 :][:4]
            self.audits.append((event, actor, server))
            self.nonces.append(nonce)
            self.events.append("audit")
            return CommandResult("", "audit write failed" if self.audit_rc else "", self.audit_rc)
        if "list-units" in command and "dango-upgrade-guard-" in command:
            lines = "".join(f"{u} loaded active waiting guard\n" for u in self.units)
            return CommandResult(lines, "", 0 if self.units else 1)
        if "ActiveEnterTimestamp" in command:
            unit = re.search(r"'([^']+)'", command)
            age = self.unit_ages.get(unit.group(1) if unit else "", 3600)
            return CommandResult("" if age is None else f"{age}\n", "", 0 if age is not None else 1)
        if command.startswith("systemd-run"):
            self.events.append("guard:arm")
            unit = re.search(r"--unit=(\S+)", command)
            self.units.append((unit.group(1) if unit else "unit") + ".timer")
            self.units.extend(self.after_arm)
            return CommandResult("", "", 0)
        if "reset-failed" in command:
            self.events.append("guard:disarm")
            self.units = []
            return CommandResult("", "", 0)
        if command.startswith("systemctl stop '") and "upgrade-guard" in command:
            self.units = [u for u in self.units if f"'{u}'" not in command]
            return CommandResult("", "", 0)
        if command == "systemctl is-active --quiet dango-web":
            return CommandResult("", "", 0 if self.web else 3)
        if command in ("systemctl start dango-web", "systemctl restart dango-web"):
            self.web = True
            self.events.append("web:start")
            return CommandResult("", "", 0)
        if "site-packages/dango/runtime/isolated_run.py" in command:
            return CommandResult("", "", 0 if self.build else 1)
        if "# runner-step:" in command:
            return self._step(command.split("# runner-step:")[1].split("\n")[0])
        return CommandResult("", "", 0)

    def _probe(self) -> str:
        return probe(
            "1" if self.active else "0",
            "1" if self.disabled else "0",
            "1" if self.nft else "0",
            record=self.record,
            disabled_info=self.disabled_info if self.disabled else None,
        )

    def _step(self, name: str) -> CommandResult:
        self.events.append(name)
        if name in self.fail:
            return CommandResult("", f"{name} broke\n{self.fail_detail}", self.fail[name])
        if name == "stop_web":
            self.web = False
        elif name == "quiesce":
            self.web = (
                False if self.web and "systemctl stop dango-web" in self.commands[-1] else self.web
            )
        elif name == "disable_marker":
            self.disabled = True
        elif name == "marker":
            self.active = True
        elif name == "remove_disabled":
            self.disabled = False
        return CommandResult("", "", 0)


def invoke(
    ssh: IsoSSH, args: list[str], input: str | None = None, key_path: str | None = None
) -> Result:
    cfg = MagicMock(droplet_ip=IP)
    if key_path is not None:
        cfg.ssh_key_path = key_path
    with (
        patch(
            "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
            return_value=(cfg, Path("/project")),
        ),
        patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        patch("getpass.getuser", return_value="alice"),
        patch("time.sleep"),
    ):
        return CliRunner().invoke(remote, ["isolation", *args], input=input)


def order(ssh: IsoSSH, *names: str) -> None:
    positions = [ssh.events.index(n) for n in names]
    assert positions == sorted(positions), (names, ssh.events)
