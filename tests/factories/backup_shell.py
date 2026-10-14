"""tests/factories/backup_shell.py

Helpers for backup/restore tests that execute the generated shell commands for real.

``ShellSSH`` stands in for ``SSHManager``: it runs each command in a local ``bash``
(so ``&&``/``||``/``if`` semantics and real ``cp`` exit codes are exercised) and
stubs only commands that need a real server (systemd, Docker, health check,
ownership, the server venv).  A ``cp`` shim on ``PATH`` can be told to fail for
one path, simulating a disk-full or permission error on that copy.

The database helpers create real DuckDB / SQLite files, including a live database
with a leftover write-ahead log from a hard-killed writer.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import duckdb

from dango.platform.cloud.ssh import CommandResult

#: Command prefixes that need a real server; they are recorded and reported as successful.
_STUBBED_PREFIXES = (
    "systemctl",
    "docker",
    "curl",
    "chown",
    "COMPOSE_PROJECT_NAME=",
    "/srv/dango/venv",
)

_CP_SHIM = """#!/bin/sh
if [ -n "$FAIL_CP_MATCH" ]; then
  for arg in "$@"; do
    case "$arg" in
      *"$FAIL_CP_MATCH"*) echo "cp: $arg: No space left on device" >&2; exit 1 ;;
    esac
  done
fi
exec /bin/cp "$@"
"""


class ShellSSH:
    """Minimal ``SSHManager`` replacement that executes commands in a local shell."""

    def __init__(self, shim_dir: Path, *, fail_cp_match: str | None = None) -> None:
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim = shim_dir / "cp"
        shim.write_text(_CP_SHIM)
        shim.chmod(0o755)
        self._env = dict(os.environ)
        self._env["PATH"] = f"{shim_dir}{os.pathsep}{self._env.get('PATH', '')}"
        if fail_cp_match:
            self._env["FAIL_CP_MATCH"] = fail_cp_match
        self.commands: list[str] = []

    def exec_command(self, command: str, timeout: int = 60, **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        if command.lstrip().startswith(_STUBBED_PREFIXES):
            return CommandResult(stdout="", stderr="", exit_code=0)
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env,
        )
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


# ---------------------------------------------------------------------------
# Real database fixtures
# ---------------------------------------------------------------------------


def make_duckdb(path: Path, value: str) -> None:
    """Create a cleanly closed DuckDB file with table ``t(v)`` holding one row *value*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t AS SELECT ? AS v", [value])
    conn.close()


def make_duckdb_with_leftover_wal(path: Path) -> Path:
    """Create a live DuckDB whose last write (table ``live_only``) exists only in its WAL.

    The writer process is hard-killed (``os._exit``) so no checkpoint runs, as after a crash.
    Returns the WAL path.
    """
    make_duckdb(path, "live")
    script = (
        "import duckdb, os, sys\n"
        "c = duckdb.connect(sys.argv[1])\n"
        "c.execute('PRAGMA disable_checkpoint_on_shutdown')\n"
        "c.execute(\"SET wal_autocheckpoint='1GB'\")\n"
        "c.execute('CREATE TABLE live_only AS SELECT 1 AS x')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    wal = Path(f"{path}.wal")
    assert wal.exists(), "fixture failed to leave a DuckDB WAL behind"
    return wal


def duckdb_state(path: Path) -> tuple[set[str], list[str]]:
    """Return (table names, values of ``t.v``) for the DuckDB file at *path*."""
    conn = duckdb.connect(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
        }
        values = [row[0] for row in conn.execute("SELECT v FROM t ORDER BY v").fetchall()]
    finally:
        conn.close()
    return tables, values


def make_sqlite(path: Path, value: str) -> None:
    """Create a SQLite file (rollback journal, no sidecars) with table ``u(n)`` holding *value*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE u (n TEXT)")
    conn.execute("INSERT INTO u VALUES (?)", (value,))
    conn.commit()
    conn.close()


def make_sqlite_with_leftover_wal(path: Path) -> tuple[Path, Path]:
    """Create a WAL-mode SQLite whose last insert (``'live'``) exists only in ``-wal``.

    Returns the ``-wal`` and ``-shm`` paths, both left behind by a hard-killed writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE u (n TEXT)")
    conn.execute("INSERT INTO u VALUES ('old')")
    conn.commit()
    conn.close()
    script = (
        "import sqlite3, os, sys\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute(\"INSERT INTO u VALUES ('live')\")\n"
        "c.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    wal, shm = Path(f"{path}-wal"), Path(f"{path}-shm")
    assert wal.exists() and shm.exists(), "fixture failed to leave SQLite sidecars behind"
    return wal, shm


def sqlite_values(path: Path) -> list[str]:
    """Return the values of ``u.n`` in the SQLite file at *path*."""
    conn = sqlite3.connect(str(path))
    try:
        return sorted(row[0] for row in conn.execute("SELECT n FROM u").fetchall())
    finally:
        conn.close()
