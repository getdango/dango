"""tests/integration/lock_holder_process.py

Stand-in for another Dango writer: a separate process that holds the dbt lock and a DuckDB write
connection to the project warehouse until told to stop (run as `python lock_holder_process.py ROOT`).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HOLD_LIMIT_SECONDS = 120  # safety net: never outlive a crashed test


def main(root: Path) -> None:
    import duckdb

    from dango.utils.dbt_lock import DbtLock

    lock = DbtLock(root, source="sync", operation="load:holder")
    lock.acquire(timeout=0)
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    conn.execute("CREATE TABLE IF NOT EXISTS holder_marker (n INTEGER)")
    (root / "holder.ready").write_text("ready", encoding="utf-8", newline="\n")
    deadline = time.monotonic() + HOLD_LIMIT_SECONDS
    while not (root / "holder.release").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    conn.close()
    lock.release()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
