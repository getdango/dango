"""tests/unit/test_dbt_failure_cause_text.py

A damaged DuckDB warehouse or WAL file gets the database cause text, and an ordinary dbt failure
does not (the old check matched the adapter line "Registered adapter: duckdb" in every run).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.transformation import run_dbt_snapshots

pytestmark = pytest.mark.unit

ADAPTER_LINE = "13:00:00  Registered adapter: duckdb=1.11.0\n"
# Wordings taken from the DuckDB 1.5.4 library (strings in the binary).
DAMAGED = [
    "Corrupt WAL file: entry size exceeded remaining data in file at byte position 120",
    "Corrupt Encrypted WAL file: entry size exceeded remaining data in file at byte position 9",
    "Corrupt database file: computed checksum 1 does not match stored checksum 2",
    "IO Error: Database file appears to be corrupted.",
    'IO Error: The file "w.duckdb" exists, but it is not a valid DuckDB database file!',
]
CAUSE = "Database file corrupted"


def _snapshot_output(tmp_path: Path, error_text: str) -> str:
    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout=ADAPTER_LINE + error_text + "\n", stderr="")

    with patch("dango.transformation.subprocess.run", side_effect=fake_run):
        ok, output = run_dbt_snapshots(tmp_path)
    assert ok is False
    return output


@pytest.mark.parametrize("text", DAMAGED)
def test_a_damaged_warehouse_gets_the_database_cause(tmp_path: Path, text: str) -> None:
    assert CAUSE in _snapshot_output(tmp_path, text)


def test_an_ordinary_failure_does_not_blame_the_database(tmp_path: Path) -> None:
    output = _snapshot_output(tmp_path, "Runtime Error in snapshot s: Conversion Error")
    assert CAUSE not in output
    assert "write lock" not in output
