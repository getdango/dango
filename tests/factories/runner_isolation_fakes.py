"""tests/factories/runner_isolation_fakes.py

A pretend root SSH session that also understands the runner-isolation steps.  Each step is
recognised by its ``# runner-step:NAME`` tag; the fake keeps the marker and the version record
so order, idempotence and the failure policy can be asserted.  Nothing here runs a shell.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from dango.platform.cloud.ssh import CommandResult
from tests.factories.compose_switch_fakes import FakeServer

RUNNER_TAG = re.compile(r"# runner-step:(\w+)")


@dataclass
class RunnerServer(FakeServer):
    """``FakeServer`` plus the state the runner-isolation steps change."""

    rfail: dict[str, int] = field(default_factory=dict)
    isolation_marker: str | None = None
    nft_loaded: bool = False
    done: set[str] = field(default_factory=set)
    runner_commands: list[str] = field(default_factory=list)
    web_active: bool = True
    build_present: bool = True

    def _answer(self, command: str) -> CommandResult:
        match = RUNNER_TAG.search(command)
        if match is None:
            return self._plain(command)
        for needle, exc in self.raise_on.items():
            if needle in command:
                self.events.append(f"raised:{needle}")
                raise exc
        name = match.group(1)
        self.events.append(f"r:{name}")
        self.runner_commands.append(command)
        if name in self.rfail:
            return CommandResult("", f"{name} broke\nlast line", self.rfail[name])
        self.done.add(name)
        if name == "quiesce" and "systemctl stop dango-web" in command:
            self.web_active = False
            self.events.append("web-stop")
        if name == "nft_rules":
            self.nft_loaded = True
        elif name == "marker":
            loopback = "restricted" if self.nft_loaded else "unrestricted"
            self.isolation_marker = json.dumps({"schema": 1, "loopback": loopback})
        elif name == "record":
            self._merge_record()
        return CommandResult("", "", 0)

    def _plain(self, command: str) -> CommandResult:
        """Commands that are not tagged steps but are part of the isolation flow."""
        if "find_spec" in command and "isolated_run" in command:
            return CommandResult("", "", 0 if self.build_present else 1)
        if command.startswith("test -f") and "runner/active" in command:
            return CommandResult("", "", 0 if self.isolation_marker is not None else 1)
        if command == "systemctl is-active --quiet dango-web":
            return CommandResult("", "", 0 if self.web_active else 3)
        if command == "systemctl stop dango-web":
            self.web_active = False
        elif command in ("systemctl start dango-web", "systemctl restart dango-web"):
            self.web_active = True
        return super()._answer(command)

    def _merge_record(self) -> None:
        if self.record is None:
            return
        data: dict[str, Any] = json.loads(self.record)
        if self.isolation_marker is None:
            data.pop("runner_isolation", None)
        else:
            marker = json.loads(self.isolation_marker)
            data["runner_isolation"] = {"schema": marker["schema"], "loopback": marker["loopback"]}
        self.record = json.dumps(data)

    def runner_events(self) -> list[str]:
        return [e[2:] for e in self.events if e.startswith("r:")]
