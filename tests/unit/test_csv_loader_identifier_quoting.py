"""tests/unit/test_csv_loader_identifier_quoting.py

CSVLoader quotes schema, table and column identifiers, so SQL keywords work as names.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest

from dango.config.models import CSVSourceConfig
from dango.ingestion.csv_loader import CSVLoader


class _RecordingConnection:
    """Delegates to a real DuckDB connection and records every SQL string."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self.sql: list[str] = []

    def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        self.sql.append(query)
        return self._conn.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.mark.unit
class TestQuoteIdentifier:
    def test_plain_name(self) -> None:
        from dango.validation import quote_identifier

        assert quote_identifier("order") == '"order"'

    def test_embedded_quote_is_doubled(self) -> None:
        from dango.validation import quote_identifier

        assert quote_identifier('a"b') == '"a""b"'

    def test_round_trips_through_duckdb(self) -> None:
        from dango.validation import quote_identifier

        conn = duckdb.connect(":memory:")
        table = quote_identifier('we"ird')
        conn.execute(f"CREATE TABLE {table} ({quote_identifier('select')} INT)")
        assert conn.execute(f"DESCRIBE {table}").fetchall()[0][0] == "select"


class _Harness:
    """Runs ``CSVLoader.load`` against a real DuckDB file, recording the SQL it executes."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db_path = tmp_path / "wh.duckdb"
        self.loader = CSVLoader(project_root=tmp_path, duckdb_path=self.db_path)
        self.config = CSVSourceConfig(directory=self.data, file_pattern="*.csv")
        self.sql: list[str] = []

    def _connect(self, *args: Any, **kwargs: Any) -> _RecordingConnection:
        spy = _RecordingConnection(duckdb.connect(str(self.db_path)))
        spy.sql = self.sql  # the loader closes its connection after each load
        return spy

    def load(self, **kwargs: Any) -> dict[str, Any]:
        with patch(
            "dango.ingestion.dlt_runner._connect_with_lock_retry", side_effect=self._connect
        ):
            return self.loader.load("order", self.config, target_schema="raw_order", **kwargs)

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        conn = duckdb.connect(str(self.db_path), read_only=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    h = _Harness(tmp_path)
    (h.data / "one.csv").write_text("select,from\n1,a\n2,b\n")
    return h


@pytest.mark.unit
class TestCsvLoaderQuotesIdentifiers:
    def test_csv_loader_quotes_identifiers(self, harness: _Harness) -> None:
        result = harness.load()

        assert result.get("success", True) is not False, result
        rows = harness.query('SELECT "select", "from" FROM "raw_order"."order" ORDER BY 1')
        assert rows == [(1, "a"), (2, "b")]
        executed = "\n".join(harness.sql)
        assert 'CREATE SCHEMA IF NOT EXISTS "raw_order"' in executed
        assert 'CREATE TABLE "raw_order"."order"' in executed

    def test_schema_evolution_and_reload_with_keyword_names(self, harness: _Harness) -> None:
        harness.load()

        (harness.data / "two.csv").write_text("select,from,where\n3,c,x\n")
        harness.load(allow_schema_changes=True)
        cols = [r[0] for r in harness.query('DESCRIBE "raw_order"."order"')]
        assert {"select", "from", "where"} <= set(cols)
        assert harness.query('SELECT COUNT(*) FROM "raw_order"."order"') == [(3,)]

        (harness.data / "one.csv").write_text("select,from\n9,z\n")
        harness.load(allow_schema_changes=True)
        assert harness.query('SELECT "select" FROM "raw_order"."order" ORDER BY 1') == [(3,), (9,)]
        assert 'ADD COLUMN "where"' in "\n".join(harness.sql)
