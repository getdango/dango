"""scripts/generate_duckdb_function_allowlist.py

Regenerate dango/transformation/duckdb_function_allowlist.txt from the installed DuckDB's catalog.
Run from the repo root after a DuckDB upgrade: python scripts/generate_duckdb_function_allowlist.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "dango" / "transformation" / "duckdb_function_allowlist.txt"
QUERY = (
    "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
    "WHERE function_type IN ('scalar', 'aggregate', 'window', 'macro') AND internal"
)


def main() -> int:
    """Write the allow-list: built-in scalar/aggregate/window/macro functions minus the deny set."""
    import duckdb

    from dango.transformation.model_sql_functions import is_denied_function

    conn = duckdb.connect(":memory:")
    try:
        names = sorted({r[0] for r in conn.execute(QUERY).fetchall()} - {""})
    finally:
        conn.close()
    allowed = [n for n in names if not is_denied_function(n)]
    header = [
        f"# DuckDB {duckdb.__version__} built-in scalar/aggregate/window/macro functions",
        "# (duckdb_functions(), internal only) minus file/URL/settings/env/variable access.",
        "# Regenerate: python scripts/generate_duckdb_function_allowlist.py",
        "# Regenerate whenever the DuckDB pin changes (pyproject allows <1.6); functions",
        "# missing from this list (e.g. added in a newer DuckDB) fail closed.",
        "# Used by dango/transformation/model_sql_guard.py for agent-written model SQL.",
    ]
    OUT.write_text("\n".join(header + allowed) + "\n", encoding="utf-8")
    print(f"wrote {len(allowed)} names ({len(names) - len(allowed)} denied) to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
