"""scripts/check_duckdb_lock_text.py

Capture DuckDB's REAL lock-conflict message on this platform and check Dango's recogniser against it.

A second process holds a database file open; this process then tries to open it (read-write and
read-only), prints the exact message DuckDB raised, and checks that Dango recognises it and that the
friendly text replaces it. Run it with the interpreter that has Dango installed. Exit code 0 when
every check passed, 1 otherwise. Used by the platform-smoke workflow (informational) so the Windows
wording is seen on a real Windows runner.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_HOLDER = (
    "import duckdb, os, sys\n"
    "conn = duckdb.connect(sys.argv[1])\n"
    "print(os.getpid(), flush=True)\n"
    "sys.stdin.read()\n"
)
_RAW_MARKERS = ("Could not set lock", "Conflicting lock", "File is already open", "IO Error")


def _real_messages(db_path: Path) -> tuple[int, dict[str, str]]:
    """The holder's pid and DuckDB's message for a read-write and a read-only open."""
    import duckdb

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        pid = int(holder.stdout.readline().strip())
        found: dict[str, str] = {}
        for label, config in (("read-write", {}), ("read-only", {"access_mode": "read_only"})):
            try:
                duckdb.connect(str(db_path), config=config)
            except duckdb.Error as exc:
                found[label] = str(exc)
            else:
                found[label] = ""
        return pid, found
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()


def main() -> int:
    """Run the check; print the real message and the friendly text."""
    import duckdb

    from dango.utils.duckdb_lock_text import describe_error, is_duckdb_lock_error

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="dango-lock-check-") as tmp:
        db_path = Path(tmp) / "warehouse.duckdb"
        duckdb.connect(str(db_path)).close()
        pid, messages = _real_messages(db_path)
        print(f"platform={sys.platform} duckdb={duckdb.__version__} holder_pid={pid}")
        for label, message in messages.items():
            print(f"REAL DUCKDB MESSAGE ({label}): {ascii(message)}")
            if not message:
                note = f"{label}: DuckDB did not refuse the second open"
                if label == "read-write":
                    failures.append(note)
                else:
                    print(f"NOTE: {note} (not a failure)")
                continue
            error = duckdb.IOException(message)
            if not is_duckdb_lock_error(error):
                failures.append(f"{label}: the recogniser does not match the real message")
                continue
            friendly = describe_error(error)
            print(f"FRIENDLY TEXT ({label}): {ascii(friendly)}")
            if "Another Dango process is using the warehouse" not in friendly:
                failures.append(f"{label}: the friendly text is missing")
            leaked = [m for m in _RAW_MARKERS if m in friendly]
            if leaked or str(db_path) in friendly:
                failures.append(f"{label}: the friendly text still shows raw wording {leaked}")
            if f"(PID {pid})" in message and f"(PID {pid})" not in friendly:
                failures.append(f"{label}: the holder pid in the message was not carried over")
    if failures:
        for failure in failures:
            print(f"MISMATCH: {failure}")
        return 1
    print("OK: the real DuckDB lock message is recognised and replaced on this platform")
    return 0


if __name__ == "__main__":
    sys.exit(main())
