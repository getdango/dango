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

    def _answer(self, command: str) -> CommandResult:
        match = RUNNER_TAG.search(command)
        if match is None:
            return super()._answer(command)
        name = match.group(1)
        self.events.append(f"r:{name}")
        self.runner_commands.append(command)
        if name in self.rfail:
            return CommandResult("", f"{name} broke\nlast line", self.rfail[name])
        self.done.add(name)
        if name == "nft_rules":
            self.nft_loaded = True
        elif name == "marker":
            loopback = "restricted" if self.nft_loaded else "unrestricted"
            self.isolation_marker = json.dumps({"schema": 1, "loopback": loopback})
        elif name == "record":
            self._merge_record()
        return CommandResult("", "", 0)

    def _merge_record(self) -> None:
        if self.record is None or self.isolation_marker is None:
            return
        data: dict[str, Any] = json.loads(self.record)
        marker = json.loads(self.isolation_marker)
        data["runner_isolation"] = {"schema": marker["schema"], "loopback": marker["loopback"]}
        self.record = json.dumps(data)

    def runner_events(self) -> list[str]:
        return [e[2:] for e in self.events if e.startswith("r:")]
