"""tests/factories/compose_switch_fakes.py

Stateful fake root SSH session for the compose-helper switch: recognises each migration step by
its ``# step:XX`` tag and keeps marker / group / pin state, so order and idempotence can be asserted.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.ssh import CommandResult

PROJECT_ID = "0123456789abcdef0123456789abcdef"
PROJECT_YML = f"project:\n  name: demo\n  id: {PROJECT_ID}\n"
_TAG = re.compile(r"# step:(\w+)")
_HELPER_VERB = re.compile(r"dango-compose'? (?P<verb>[a-z0-9-]+)")


def step_of(command: str) -> str | None:
    """The ``# step:XX`` tag of a generated script, if any."""
    match = _TAG.search(command)
    return match.group(1) if match else None


@dataclass
class FakeServer:
    """Root SSH session on a pretend server; ``events`` is the ordered semantic log."""

    version: str = "1.0.14"
    marker: bool = False
    in_group: bool = True
    pinned: str | None = None
    project: bool = True
    fail: dict[str, int] = field(default_factory=dict)
    remain: bool = False
    sudoers: bool = False
    blind: bool = False
    record: str | None = None
    web_unit: str = ""
    events: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    timeouts: list[int | None] = field(default_factory=list)
    written: dict[str, str] = field(default_factory=dict)
    stop_rc: list[int] = field(default_factory=list)
    target: str | None = None
    raise_on: dict[str, Exception] = field(default_factory=dict)

    # --- SSHManager surface ----------------------------------------------------------
    def connect(self, *_a: Any, **_k: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def write_remote_file(self, path: str, content: str, mode: int = 0o644) -> None:
        self.written[path] = content
        self.events.append(f"write:{path}")

    def exec_command(self, command: str, timeout: int | None = None, **_k: Any) -> CommandResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        return self._answer(command)

    # --- helpers -----------------------------------------------------------------------
    def _ok(self, out: str = "") -> CommandResult:
        return CommandResult(out, "", 0)

    def _bad(self, tag: str) -> CommandResult:
        return CommandResult("", f"{tag} failed", self.fail[tag])

    def _answer(self, command: str) -> CommandResult:
        for needle, exc in self.raise_on.items():
            if needle in command:
                self.events.append(f"raised:{needle}")
                raise exc
        tag = step_of(command)
        if tag is not None:
            return self._step(tag, command)
        if command.startswith("test -s") and "project-id" in command:
            return CommandResult("", "", 0 if self.pinned else 1)
        if command.startswith("test -e") and ch.SUDOERS_PATH in command:
            return CommandResult("", "", 0 if self.sudoers else 1)
        if command.startswith("test -f") and "project.yml" in command:
            return CommandResult("", "", 0 if self.project else 1)
        if command == f"{ch.HELPER_PATH} build":
            self.events.append("build")
            return self._bad("build") if "build" in self.fail else self._ok()
        if command == ch.MARKER_TEST:
            return CommandResult("", "", 0 if self.marker else 1)
        if "project.yml" in command and "runuser" in command:
            return self._ok(PROJECT_YML if self.project else "")
        if command == "systemctl stop dango-web":
            self.events.append("web-stop")
            return self._ok()
        if command in ("systemctl start dango-web", "systemctl restart dango-web"):
            self.events.append("web-start")
            return self._ok()
        if "cat /etc/systemd/system/dango-web.service" in command:
            return self._ok(self.web_unit)
        if "import dango" in command:
            return self._ok(self.version)
        if "server-version.json" in command and "marker=" in command:
            return self._ok(self._access_probe())
        if "venv/bin/pip install" in command:
            self.events.append("pip")
            match = re.search(r"getdango==(\S+)", command)
            self.version = match.group(1) if match else self.target or self.version
            return self._ok()
        if command.startswith("systemd-run"):
            self.events.append("guard-arm")
            return self._ok()
        if "dango-upgrade-guard" in command and "systemctl stop" in command:
            self.events.append("guard-disarm")
            return self._ok()
        if f"{ch.HELPER_PATH} pin-project" in command:
            return self._pin(command)
        verbs = _HELPER_VERB.findall(command)
        if verbs and ch.MARKER_TEST in command:
            return self._wrapped(verbs, command)
        if "usermod -aG docker" in command:
            self.events.append("usermod")
            self.in_group = True
            return self._ok()
        return self._ok()

    def _access_probe(self) -> str:
        if self.blind:
            return "garbled"
        groups = "dango docker" if self.in_group else "dango"
        rec = self.record or ""
        return f"marker={int(self.marker)}\ngroups={groups}\n---\n{rec}"

    def _pin(self, command: str) -> CommandResult:
        if "pin" in self.fail:
            return self._bad("pin")
        self.pinned = shlex.split(command)[-1]
        self.events.append("pin-project")
        return self._ok()

    def _wrapped(self, verbs: list[str], command: str) -> CommandResult:
        for verb in verbs:
            self.events.append(f"{'helper' if self.marker else 'legacy'}:{verb}")
            if verb in self.fail:
                return self._bad(verb)
            if verb == "stop" and self.marker and self.stop_rc:
                code = self.stop_rc.pop(0)
                if code:
                    return CommandResult("", "busy", code)
        return self._ok()

    def _step(self, tag: str, command: str) -> CommandResult:
        self.events.append(tag)
        if tag in self.fail:
            return self._bad(tag)
        if tag == "P1":
            state = (
                "none"
                if self.pinned is None
                else ("same" if self.pinned == PROJECT_ID else "other")
            )
            return self._ok(f"PINNED={state}\n")
        if tag == "M":
            self.marker = True
            self.record = _record_of(command)
        elif tag == "REC":
            self.record = _record_of(command)
        elif tag == "GM":
            return CommandResult("", "", 0 if self.in_group else 1)
        elif tag == "G":
            if self.in_group:
                self.events.append("gpasswd")
                self.in_group = False
        elif tag == "T":
            if self.remain:
                return CommandResult("T_REMAIN\n", "", 3)
            return self._ok("T_OK\n")
        elif tag == "FB":
            self.marker = False
            self.in_group = True
            self.record = _record_of(command)
        elif tag == "R":
            self.sudoers = False
            self.in_group = True
            self.marker = False
            self.record = _record_of(command)
        return self._ok()


def _record_of(command: str) -> str | None:
    match = re.search(r'\{"dango": "[^"]*", "docker_access": "[^"]*"\}', command)
    return match.group(0) if match else None
