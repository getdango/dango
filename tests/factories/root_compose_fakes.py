"""tests/factories/root_compose_fakes.py

Recording fake root SSH session for the compose-helper tests: understands the
``server_compose_cmd`` wrapper (helper branch when the marker exists, legacy branch otherwise).
"""

from __future__ import annotations

import re
from typing import Any

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.ssh import CommandResult

_WRAPPER = re.compile(
    r"^if (?P<test>.+?); then (?P<helper>.*?); else (?P<legacy>.*); fi$",
    re.DOTALL,
)
#: The marker probe the callers run once per operation (exit 0 = helper branch is active).
_PROBE_PREFIX = '[ -n "$(find '


def is_marker_probe(command: str) -> bool:
    """True for the standalone marker/ownership test (not a wrapped command)."""
    return command.startswith(_PROBE_PREFIX) and split_wrapper(command) is None


def split_wrapper(command: str) -> tuple[str, str] | None:
    """``(helper branch, legacy branch)`` of a ``server_compose_cmd`` string, else None."""
    match = _WRAPPER.match(command)
    return (match.group("helper"), match.group("legacy")) if match else None


class FakeRootSSH:
    """Records commands and a coarse event log; answers helper verbs from ``rc``.

    ``rc`` maps a helper verb string (``"stop metabase"``) to one exit code or a list used
    in call order (the last one repeats).  Exit 75 is the helper's "busy".
    """

    def __init__(
        self,
        *,
        helper: bool,
        rc: dict[str, int | list[int]] | None = None,
        volume: str = "/var/lib/docker/volumes/dango-ab12cd34_metabase-data/_data",
    ) -> None:
        self.helper = helper
        self.rc = {k: ([v] if isinstance(v, int) else list(v)) for k, v in (rc or {}).items()}
        self.volume = volume
        self.commands: list[str] = []
        self.timeouts: list[int | None] = []
        self.events: list[str] = []
        self.probes = 0

    # --- SSHManager surface used by the callers -------------------------------------
    def connect(self, _ip: str, **_kw: Any) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def exec_command(self, command: str, timeout: int | None = None, **_kw: Any) -> CommandResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if is_marker_probe(command):
            self.probes += 1
            return CommandResult("", "", 0 if self.helper else 1)
        branches = split_wrapper(command)
        if branches is not None:
            helper_cmd, legacy_cmd = branches
            return self._helper(helper_cmd) if self.helper else self._plain(legacy_cmd)
        return self._plain(command)

    # --- internals ----------------------------------------------------------------
    def _next_rc(self, verb: str) -> int:
        codes = self.rc.get(verb)
        if not codes:
            return 0
        return codes.pop(0) if len(codes) > 1 else codes[0]

    def _helper(self, helper_cmd: str) -> CommandResult:
        assert helper_cmd.startswith(ch.HELPER_PATH + " "), helper_cmd
        verb = helper_cmd[len(ch.HELPER_PATH) + 1 :]
        code = self._next_rc(verb)
        self.events.append(f"helper:{verb}:{code}")
        out = self.volume + "\n" if verb == "volume-path" and code == 0 else ""
        err = "dango-compose: busy" if code == 75 else ""
        return CommandResult(stdout=out, stderr=err, exit_code=code)

    def _plain(self, command: str) -> CommandResult:
        if command.startswith("systemctl stop dango-web"):
            self.events.append("web:stop")
        elif command.startswith("systemctl start dango-web") or command.startswith(
            "systemctl restart dango-web"
        ):
            self.events.append("web:start")
        elif "docker compose" in command or "docker volume" in command:
            self.events.append(f"legacy:{command}")
            if "docker volume inspect" in command:
                return CommandResult(stdout=self.volume + "\n", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    # --- assertions helpers ---------------------------------------------------------
    def timeout_of(self, needle: str) -> int | None:
        """Timeout of the first command containing *needle*."""
        return next(t for c, t in zip(self.commands, self.timeouts, strict=True) if needle in c)

    def helper_branches(self) -> list[str]:
        """The helper branch of every wrapped command."""
        return [b[0] for c in self.commands if (b := split_wrapper(c)) is not None]


def assert_no_project_compose_on_helper_branch(ssh: FakeRootSSH) -> None:
    """V3: no command reaches the project compose file except inside a legacy ``else`` branch."""
    for command in ssh.commands:
        branches = split_wrapper(command)
        if branches is None:
            assert "docker compose" not in command, command
            assert "docker volume" not in command, command
            continue
        helper_branch = branches[0]
        assert "docker compose" not in helper_branch, command
        assert "/srv/dango/project" not in helper_branch, command
        assert "cd /srv/dango/project" not in helper_branch, command
