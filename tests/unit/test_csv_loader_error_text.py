"""tests/unit/test_csv_loader_error_text.py

CSV loader failure messages print literally even when a cell value or column looks like markup.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from rich.console import Console

from dango.config.models import CSVSourceConfig
from dango.ingestion.csv_loader import CSVLoader

pytestmark = pytest.mark.unit


@pytest.fixture
def out() -> Iterator[StringIO]:
    buf = StringIO()
    with patch("dango.ingestion.csv_loader.console", Console(file=buf, width=2000)):
        yield buf


def _loader(tmp_path: Path) -> tuple[CSVLoader, Path]:
    db = tmp_path / "data" / "warehouse.duckdb"
    db.parent.mkdir()
    data = tmp_path / "csv_data"
    data.mkdir()
    return CSVLoader(tmp_path, db), data


def _bad_csv(path: Path) -> None:
    # the bad cell lies past DuckDB's type-sniffing sample, so the load raises a Conversion Error
    rows = ["a,b"] + [f"{i},{i}" for i in range(30000)] + ["30001,see [/x] note"]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_load_failure_with_markup_like_cell_is_reported(tmp_path: Path, out: StringIO) -> None:
    loader, data = _loader(tmp_path)
    bad = data / "b.csv"
    _bad_csv(bad)
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t (a BIGINT, b BIGINT)")
    ok = loader._load_new_file(conn, str(bad), "t", "orders", skip_schema_check=True)
    assert ok is False
    assert "see [/x] note" in out.getvalue()


def test_reload_and_delete_failures_are_escaped(tmp_path: Path, out: StringIO) -> None:
    loader, data = _loader(tmp_path)
    bad = data / "b.csv"
    _bad_csv(bad)
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t (a BIGINT, b BIGINT)")
    assert (
        loader._reload_updated_file(conn, str(bad), "t", "orders", skip_schema_check=True) is False
    )
    boom = MagicMock()
    boom.execute.side_effect = RuntimeError("bad [/x] value")
    assert loader._delete_file_data(boom, str(bad), "t", "orders") is False
    printed = out.getvalue()
    assert "see [/x] note" in printed and "bad [/x] value" in printed


def test_schema_mismatch_with_markup_like_column_is_reported(tmp_path: Path, out: StringIO) -> None:
    loader, data = _loader(tmp_path)
    config = CSVSourceConfig(directory=data, file_pattern="*.csv")
    (data / "a.csv").write_text("id,amount\n1,2\n", encoding="utf-8")
    assert loader.load("src", config, "raw_src")["status"] == "success"
    (data / "b.csv").write_text("id,[/x]col\n1,2\n", encoding="utf-8")
    result = loader.load("src", config, "raw_src")
    assert result["status"] == "error"
    assert "[/x]col" in out.getvalue()
