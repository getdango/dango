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
for last; do :; done
if [ -n "$CP_LOG" ]; then printf '%s\\t%s\\n' "${RUNUSER_AS:-root}" "$last" >> "$CP_LOG"; fi
if [ -n "$FAIL_CP_MATCH" ]; then
  for arg in "$@"; do
    case "$arg" in
      *"$FAIL_CP_MATCH"*) echo "cp: $arg: No space left on device" >&2; exit 1 ;;
    esac
  done
fi
exec /bin/cp "$@"
"""

#: ``runuser -u USER -- CMD...``: runs CMD as the current user, exporting RUNUSER_AS=USER.
_RUNUSER_SHIM = """#!/bin/sh
[ "$1" = "-u" ] && [ "$3" = "--" ] || { echo "runuser shim: unexpected usage: $*" >&2; exit 2; }
RUNUSER_AS="$2"; export RUNUSER_AS
shift 3
exec "$@"
"""

#: There is no ``dango`` user locally; ownership changes are no-ops.
_CHOWN_SHIM = """#!/bin/sh
exit 0
"""


def shim_env(shim_dir: Path, *, fail_cp_match: str | None = None) -> dict[str, str]:
    """Install the cp/runuser/chown shims in *shim_dir*; return an env that uses them.

    Every ``cp`` appends ``<user>\\t<destination>`` to ``shim_dir/cp.log`` (user is
    ``root`` unless run through ``runuser``); read it with :func:`cp_log`.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("cp", _CP_SHIM), ("runuser", _RUNUSER_SHIM), ("chown", _CHOWN_SHIM)):
        shim = shim_dir / name
        shim.write_text(body)
        shim.chmod(0o755)
    env = dict(os.environ)
    env.pop("RUNUSER_AS", None)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["CP_LOG"] = str(shim_dir / "cp.log")
    if fail_cp_match:
        env["FAIL_CP_MATCH"] = fail_cp_match
    return env


def cp_log(shim_dir: Path) -> list[tuple[str, str]]:
    """Return (user, destination) for every ``cp`` run with :func:`shim_env`'s environment."""
    log = shim_dir / "cp.log"
    if not log.exists():
        return []
    entries = []
    for line in log.read_text().splitlines():
        user, _, dest = line.partition("\t")
        entries.append((user, dest))
    return entries


class ShellSSH:
    """Minimal ``SSHManager`` replacement that executes commands in a local shell."""

    def __init__(self, shim_dir: Path, *, fail_cp_match: str | None = None) -> None:
        self.shim_dir = shim_dir
        self._env = shim_env(shim_dir, fail_cp_match=fail_cp_match)
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


# ---------------------------------------------------------------------------
# Spaces (on-server) restore harness
# ---------------------------------------------------------------------------


def run_spaces_restore(
    tmp_path: Path,
    project: Path,
    top: str,
    *,
    metabase_vol: Path | None = None,
    fail_cp_match: str | None = None,
) -> tuple[Path, list[str]]:
    """Run ``restore_from_spaces`` against *project* with ``tmp_path/top`` as the extracted staging dir.

    Shell steps run for real under :func:`shim_env` (download, extraction and the final
    ``chown`` are skipped).  Returns the shim dir (for :func:`cp_log`) and the event list
    (shell commands passed to ``subprocess.run`` and ``"start"`` for the service restart).
    """
    from unittest.mock import patch

    sb = "dango.platform.cloud.scheduled_backup"
    from dango.platform.cloud import scheduled_backup

    shim_dir = tmp_path / "shim"
    env = shim_env(shim_dir, fail_cp_match=fail_cp_match)
    events: list[str] = []
    real_path = Path
    real_run = subprocess.run

    def fake_path(value: object = ".") -> Path:
        text = str(value)
        if text.startswith("/tmp/"):
            return tmp_path / text[len("/tmp/") :]
        return real_path(str(value))

    def run_local(command: str, **_kw: object) -> str:
        from dango.exceptions import CloudProvisioningError

        if "tar -xzf" in command or command.startswith("chown"):
            return ""
        proc = real_run(command, shell=True, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise CloudProvisioningError(f"Step failed (exit {proc.returncode}): {proc.stderr}")
        return proc.stdout

    with (
        patch(f"{sb}.PROJECT_DIR", project),
        patch(f"{sb}.BACKUP_DIR", tmp_path / "backups"),
        patch(f"{sb}.download_from_spaces"),
        patch(f"{sb}.Path", fake_path),
        patch(f"{sb}._stop_services"),
        patch(f"{sb}._start_services", side_effect=lambda: events.append("start")),
        patch(f"{sb}._run_local", side_effect=run_local),
        patch(
            f"{sb}._get_metabase_volume_path",
            return_value=str(metabase_vol) if metabase_vol else None,
        ),
        patch(f"{sb}.subprocess.run", side_effect=lambda cmd, **_k: events.append(str(cmd))),
    ):
        scheduled_backup.restore_from_spaces({}, f"backups/{top}.tar.gz")
    return shim_dir, events
