"""scripts/journey_support.py

Shared helpers for scripts/first_user_journey.py: running a step's command with a log, step
results, and ASCII-safe reporting. Standard library only; safe on Windows, macOS and Linux.
"""

import locale
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

FAIL_TAIL_LINES = 15
DEFAULT_BUDGET_SECONDS = 900
BUDGET_EXHAUSTED = "journey time budget exhausted"
CLEANUP_TIMEOUT_SECONDS = 120  # cap for commands that run after the budget is spent
THROWAWAY_ADMIN_PASSWORD = "Smoke-Test-9xQ!pL3z"  # disposable journey project; not a secret


def text_of(data: str | bytes | None) -> str:
    """Return captured output as text; TimeoutExpired carries bytes on POSIX."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def safe(text: str) -> str:
    """Escape non-ASCII so printing cannot fail under a cp1252 stdout."""
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def free_ports(count: int) -> list[int]:
    """Return `count` distinct currently free loopback ports (probe, then release)."""
    socks: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            socks.append(sock)
        return [s.getsockname()[1] for s in socks]
    finally:
        for sock in socks:
            sock.close()


def port_is_open(port: int) -> bool:
    """True when something accepts connections on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Proc:
    """Result of one command run."""

    returncode: int | None  # None when it could not start or timed out
    output: str
    error: str | None = None  # set when it could not start or timed out


@dataclass
class Step:
    """Outcome of one journey step."""

    name: str
    status: str  # PASS, FAIL or SKIP
    detail: str = ""
    tail: str = ""
    flagged: str = ""  # rows of a check table that failed or warned, one per line


@dataclass
class Journey:
    """Context shared by every step: executables, environment and the log file."""

    dango: str
    python: str
    workdir: Path
    log: Path
    step_timeout: float
    isolate_keyring: bool
    budget: float = DEFAULT_BUDGET_SECONDS
    utf8_mode: bool = False
    legacy_stdio: bool = False
    port_free_timeout: float = 10
    up_timeout: float = 60
    output_encoding_override: str | None = None
    fixed_ports: tuple[int, ...] = ()  # tests: 4 project ports, then the web port
    counted: tuple[str, int] | None = None  # (schema.table, rows) seen by the count step
    results: dict[str, Step] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        """Seconds left of the total time budget (never negative)."""
        return max(0.0, self.budget - (time.monotonic() - self.started))

    def env(self) -> dict[str, str]:
        """Environment for every child process of the journey."""
        env = dict(os.environ)
        env["DO_NOT_TRACK"] = "1"
        env["DANGO_ADMIN_PASSWORD"] = THROWAWAY_ADMIN_PASSWORD
        env["PYTHONUNBUFFERED"] = "1"
        env["NO_COLOR"] = "1"  # plain text in the pasted log
        env["TERM"] = "dumb"
        env["COLUMNS"] = "200"  # rich tables are not wrapped at the 80 columns of a CI console
        # Stdio is UTF-8 as on a real console; file encodings stay at the platform default on
        # purpose, so file-encoding defects surface. --utf8-mode and --legacy-stdio change that.
        if self.legacy_stdio:
            env.pop("PYTHONIOENCODING", None)
        else:
            env["PYTHONIOENCODING"] = "utf-8"
        if self.utf8_mode:
            env["PYTHONUTF8"] = "1"
        if self.isolate_keyring:
            env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        return env

    def output_encoding(self) -> str:
        """Encoding the dango children write their piped output in.

        UTF-8 normally (PYTHONIOENCODING=utf-8, or Python UTF-8 mode). With --legacy-stdio and
        no UTF-8 mode a child falls back to this platform's preferred (ANSI) encoding.
        """
        if self.output_encoding_override:
            return self.output_encoding_override
        if self.legacy_stdio and not self.utf8_mode:
            return locale.getpreferredencoding(False)
        return "utf-8"

    def note(self, line: str) -> None:
        """Append a free-text line to the log (UTF-8)."""
        with open(self.log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def run(
        self,
        argv: list[str],
        timeout: float | None = None,
        ignore_budget: bool = False,
        cwd: Path | None = None,
        env_extra: dict[str, str] | None = None,
        env_remove: tuple[str, ...] = (),
    ) -> Proc:
        """Run a command in the workdir, append command, exit code and output to the log.

        The timeout is capped by what is left of the total budget, except for cleanup commands
        (ignore_budget=True) that must still run once the budget is spent. `cwd` replaces the
        workdir, `env_extra` adds to the journey environment and `env_remove` drops variables from
        it (by name, ignoring case: Windows upper-cases environment names) (the installer step uses all three).
        """
        limit = timeout if timeout is not None else self.step_timeout
        if ignore_budget:
            limit = min(limit, CLEANUP_TIMEOUT_SECONDS)
        else:
            limit = min(limit, self.remaining())
            if limit <= 0:
                self.note(f"$ {' '.join(argv)}\n[not run: {BUDGET_EXHAUSTED}]")
                return Proc(None, "", BUDGET_EXHAUSTED)
        self.note(f"$ {' '.join(argv)}")
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd or self.workdir,
                env={
                    k: v
                    for k, v in {**self.env(), **(env_extra or {})}.items()
                    if k.upper() not in {n.upper() for n in env_remove}
                },
                capture_output=True,
                text=True,
                encoding=self.output_encoding(),
                errors="replace",
                timeout=limit,
            )
        except subprocess.TimeoutExpired as exc:
            output = text_of(exc.stdout) + text_of(exc.stderr)
            self.note(f"[timed out after {limit:g}s]\n{output}")
            return Proc(None, output, f"timed out after {limit:g}s")
        except OSError as exc:
            self.note(f"[could not run: {exc}]")
            return Proc(None, "", f"could not run {argv[0]}: {exc}")
        output = text_of(proc.stdout) + text_of(proc.stderr)
        self.note(f"[exit code {proc.returncode}]\n{output}")
        return Proc(proc.returncode, output)

    def dango_run(self, *args: str, ignore_budget: bool = False) -> Proc:
        """Run the dango executable with `args` in the workdir."""
        return self.run([self.dango, *args], ignore_budget=ignore_budget)


_ROW = re.compile(
    r"^\s+(?P<check>\S.*?)\s{2,}(?:\S\s)?(?P<status>FAIL|WARN)(?:\s+(?P<detail>.*))?$"
)
_CATEGORY = re.compile(r"^\S[^|+\u2500-\u257f]*$")


def flagged_rows(output: str) -> list[tuple[str, str]]:
    """Rows of a `dango validate` table whose status is FAIL or WARN: (status, 'Category: row').

    The table prints a category heading line, then one row per check ("check  <mark> FAIL  text").
    """
    rows: list[tuple[str, str]] = []
    category = ""
    for line in output.splitlines():
        match = _ROW.match(line)
        if match:
            text = f"{match['check']}: {match['detail'] or ''}".rstrip(": ")
            rows.append((match["status"], f"{category}/{text}" if category else text))
        elif _CATEGORY.match(line) and len(line.split()) <= 3:
            category = line.strip()
    return rows


def failure(name: str, reason: str, output: str = "", flagged: str = "") -> Step:
    """Build a FAIL step with the last lines of `output` as its tail."""
    tail = "\n".join(output.splitlines()[-FAIL_TAIL_LINES:])
    return Step(name, "FAIL", reason, tail, flagged)


def report(step: Step) -> None:
    """Print one PASS/FAIL/SKIP line (plus a tail for failures), ASCII-safe."""
    suffix = f": {step.detail}" if step.detail else ""
    print(safe(f"{step.status} {step.name}{suffix}"), flush=True)
    if step.status == "FAIL" and step.flagged:
        print("--- flagged checks ---")
        # Indented so a `^(PASS|FAIL|SKIP) ` grep of the output only sees real step lines.
        print(safe("\n".join("  " + ln for ln in step.flagged.splitlines())))
        print("---", flush=True)
    if step.status == "FAIL" and step.tail:
        print("--- last output lines ---")
        print(safe(step.tail), flush=True)
