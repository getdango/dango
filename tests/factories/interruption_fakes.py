"""tests/factories/interruption_fakes.py

Stateful fake SSH server and kill helpers for the interruption ("run it twice") matrix tests.
"""

from __future__ import annotations

import itertools
import json
import re
import shlex
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

from dango.platform.cloud.ssh import CommandResult

LOCK_PATH = "/srv/dango/deploy.lock"
JOURNAL_PATH = "/srv/dango/project/.dango/state/deployments.jsonl"
BACKUP_DIR = "/srv/dango/backups/deploy"

Event = tuple[str, str]


@contextmanager
def distinct_backup_names() -> Iterator[None]:
    """Give every backup/restore stamp a new second, and make ``sleep`` instant."""
    counter = itertools.count(1)
    fake_time = SimpleNamespace(
        monotonic=time.monotonic,
        sleep=lambda _seconds: None,
        strftime=lambda _fmt: f"20260101-{next(counter):06d}",
    )
    with patch("dango.platform.cloud.backup.time", fake_time):
        yield


class ConnectionLost(Exception):
    """The SSH session is gone: every later command on it fails."""


class Killed(BaseException):
    """Stands in for Ctrl-C / SIGTERM: not an ``Exception``, so ``except Exception`` misses it."""


def kill_on_event(target: Event | None) -> Callable[[str, str], None]:
    """Return an ``on_progress`` callback that raises :class:`Killed` at *target*."""

    def callback(step: str, status: str) -> None:
        if target is not None and (step, status) == target:
            raise Killed(f"killed at {step}:{status}")

    return callback


class EventRecorder:
    """``on_progress`` callback that records every ``(step, status)`` event, optionally killing."""

    def __init__(self, kill_at: Event | None = None, drop: FakeServer | None = None) -> None:
        self.events: list[Event] = []
        self._kill_at = kill_at
        self._drop = drop  # server whose connection dies with the client

    def __call__(self, step: str, status: str) -> None:
        self.events.append((step, status))
        if self._kill_at is not None and (step, status) == self._kill_at:
            if self._drop is not None:
                self._drop.lost = True
            raise Killed(f"killed at {step}:{status}")


def distinct_events(events: list[Event]) -> list[Event]:
    """Events in first-seen order without duplicates (a step name can repeat across callers)."""
    seen: set[Event] = set()
    ordered: list[Event] = []
    for event in events:
        if event not in seen:
            seen.add(event)
            ordered.append(event)
    return ordered


@dataclass
class KillRule:
    """Kill during the *nth* command whose text contains *pattern*."""

    pattern: str
    nth: int = 1
    when: Literal["before", "partial", "after"] = "after"
    drop_connection: bool = False  # every later command then raises ConnectionLost
    seen: int = 0


@dataclass
class FakeServer:
    """In-memory model of the parts of a Dango server that backup/deploy commands touch.

    Anything it does not model succeeds with empty output, like the existing SSH mocks.
    """

    lock: str | None = None
    web_active: bool = True
    metabase_up: bool = True
    journal: list[str] = field(default_factory=list)
    archives: dict[str, str] = field(default_factory=dict)  # path -> "complete" | "partial"
    manifests: dict[str, str] = field(default_factory=dict)  # path -> manifest JSON
    files: dict[str, str] = field(default_factory=dict)
    stale: set[str] = field(default_factory=set)  # partial archives old enough to delete
    lost: bool = False
    staging: set[str] = field(default_factory=set)
    lock_age_seconds: int | None = None
    live_data: str = "live"
    commands: list[str] = field(default_factory=list)
    kill: KillRule | None = None
    fail_patterns: list[str] = field(default_factory=list)
    _counter: int = 0
    _order: list[str] = field(default_factory=list)

    # ---- SSHManager surface -------------------------------------------------------------
    def reconnect(self) -> None:
        self.lost = False

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        if self.lost:
            raise ConnectionLost("SSH connection lost")
        self.commands.append(command)
        rule = self.kill
        hit = rule is not None and rule.pattern in command
        if hit and rule is not None:
            rule.seen += 1
            hit = rule.seen == rule.nth
        if hit and rule is not None and rule.drop_connection:
            self.lost = rule.when == "before" or self.lost
        if hit and rule is not None and rule.when == "before":
            raise Killed(f"killed before: {rule.pattern}")
        partial = hit and rule is not None and rule.when == "partial"
        result = self._apply(command, partial=partial)
        if hit and rule is not None:
            self.lost = self.lost or rule.drop_connection
            raise Killed(f"killed during: {rule.pattern}")
        return result

    # ---- helpers ------------------------------------------------------------------------
    def _ok(self, stdout: str = "") -> CommandResult:
        return CommandResult(stdout=stdout, stderr="", exit_code=0)

    def _fail(self, stderr: str = "") -> CommandResult:
        return CommandResult(stdout="", stderr=stderr, exit_code=1)

    def _quoted_payload(self, command: str, redirect: str) -> str:
        head = command.split("echo ", 1)[1]
        return shlex.split(head[: head.rindex(f" {redirect}")])[0]

    def _apply(self, cmd: str, *, partial: bool) -> CommandResult:
        for pattern in self.fail_patterns:
            if pattern in cmd:
                return self._fail("injected failure")
        if cmd.startswith("cat ") and cmd.endswith(".json 2>/dev/null"):
            path = shlex.split(cmd)[1]
            return self._ok(self.manifests[path]) if path in self.manifests else self._fail()
        if cmd.startswith("cat > ") and "MANIFEST_EOF" in cmd:
            target = shlex.split(cmd.split(" << ")[0])[2]
            self.files[target] = cmd.split("\n", 1)[1].rsplit("\nMANIFEST_EOF", 1)[0]
            return self._ok()
        if cmd.startswith(f"cat {LOCK_PATH}"):
            return self._ok(self.lock) if self.lock is not None else self._fail()
        if "stat -c %Y" in cmd and LOCK_PATH in cmd:
            if self.lock is None or self.lock_age_seconds is None:
                return self._fail()
            return self._ok(f"{self.lock_age_seconds}\n")
        if cmd.startswith(f'[ "$(cat {LOCK_PATH}'):
            ours = shlex.split(cmd.split(" = ", 1)[1].split(" && ")[0])[0]
            if self.lock != ours:
                return self._fail()
            self.lock = None
            return self._ok()
        if cmd == f"rm -f {LOCK_PATH}":
            self.lock = None
            return self._ok()
        if cmd.startswith("(set -C; echo ") and f"> {LOCK_PATH}" in cmd:
            if self.lock is not None:
                return self._fail("cannot overwrite existing file")
            self.lock = self._quoted_payload(cmd, f"> {LOCK_PATH}")
            return self._ok()
        if f">> {JOURNAL_PATH}" in cmd:
            self.journal.append(self._quoted_payload(cmd, f">> {JOURNAL_PATH}"))
            return self._ok()
        if cmd.startswith(f"tail -1 {JOURNAL_PATH}"):
            return self._ok(self.journal[-1]) if self.journal else self._ok()
        if cmd == "systemctl stop dango-web":
            self.web_active = False
            return self._ok()
        if cmd in ("systemctl start dango-web", "systemctl restart dango-web"):
            self.web_active = True
            return self._ok()
        if "docker compose" in cmd and " stop metabase" in cmd:
            self.metabase_up = False
            return self._ok()
        if "docker compose" in cmd and " start metabase" in cmd:
            self.metabase_up = True
            return self._ok()
        if cmd.startswith("curl -sf http://localhost:8800/api/health"):
            return self._ok() if self.web_active else self._fail()
        if cmd.startswith("df -m"):
            return self._ok("/dev/vda1 50000 10000 40000 20% /srv")
        if cmd.startswith("mktemp -d"):
            self._counter += 1
            args = shlex.split(cmd)[2:]
            template = next((a for a in args if a.startswith("/")), "/tmp/dango-tmp.XXXXXXXX")
            path = template.rstrip("X") + f"{self._counter:08d}"
            self.staging.add(path)
            return self._ok(path + "\n")
        if "tar -czf -" in cmd and ".tar.gz" in cmd:
            match = re.search(rf"> ({re.escape(BACKUP_DIR)}/\S+?\.tar\.gz(?:\.\w+)?)\)", cmd)
            assert match, cmd
            path = match.group(1)
            if path in self.archives:
                return self._fail("cannot overwrite existing file")
            self.archives[path] = "partial" if partial else "complete"
            self._order.append(path)
            return self._ok()
        if "mv " in cmd and BACKUP_DIR in cmd:
            paths = re.findall(rf"{re.escape(BACKUP_DIR)}/[^\s'\"]+", cmd)
            src = next(p for p in paths if p.endswith(".partial"))
            dest = next(p for p in paths if p.endswith(".tar.gz"))
            if dest in self.archives or src not in self.archives:
                return self._fail("destination exists")
            self.archives[dest] = self.archives.pop(src)
            self._order = [dest if p == src else p for p in self._order]
            return self._ok()
        if f"> {BACKUP_DIR}/" in cmd and cmd.rstrip(")").endswith(".json"):
            source = cmd.split("cat ", 1)[1].split(" >", 1)[0]
            self.manifests[cmd.rstrip(")").rsplit("> ", 1)[1]] = self.files.get(
                source.strip("'"), "{}"
            )
            return self._ok()
        if "-name 'backup-*.tar.gz.partial'" in cmd and "-delete" in cmd:
            for path in sorted(self.stale):
                self.archives.pop(path, None)
            return self._ok()
        if cmd.startswith(f"ls -1t {BACKUP_DIR}/*.tar.gz"):
            newest_first = [
                p for p in reversed(self._order) if p in self.archives and p.endswith(".tar.gz")
            ]
            return self._ok("\n".join(newest_first) + ("\n" if newest_first else ""))
        if cmd.startswith("stat --format='%s'"):
            return self._ok("1000")
        if cmd.startswith("rm -f ") and ".tar.gz" in cmd:
            for token in shlex.split(cmd)[2:]:
                self.archives.pop(token, None)
                self.manifests.pop(token, None)
            return self._ok()
        if cmd.startswith("rm -rf "):
            for token in shlex.split(cmd)[2:]:
                self.staging.discard(token)
                self.archives.pop(token, None)
            return self._ok()
        if cmd.startswith("mkdir -p /tmp/dango-restore-staging-"):
            self.staging.add(shlex.split(cmd.split("&&")[0])[-1])
            return self._ok()
        if "runuser" in cmd and " cp " in cmd and "/srv/dango/project/data/warehouse.duckdb" in cmd:
            self.live_data = "restored"
            return self._ok()
        return self._ok()

    # ---- assertions ---------------------------------------------------------------------
    def complete_archives(self) -> list[str]:
        return [p for p, state in self.archives.items() if state == "complete"]

    def partial_archives(self) -> list[str]:
        """Archive-like files that are not a finished ``*.tar.gz`` (a killed ``tar`` leaves one)."""
        return [
            p
            for p, state in self.archives.items()
            if state == "partial" or not p.endswith(".tar.gz")
        ]

    def journal_records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.journal]
