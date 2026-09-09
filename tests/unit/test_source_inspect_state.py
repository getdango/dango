"""tests/unit/test_source_inspect_state.py

Tests for `dango source inspect-state` — a read-only diagnostic command that
decodes and displays dlt's internal incremental cursor state
(base64 -> zlib -> JSON) from the destination's _dlt_pipeline_state table,
plus whether a local dlt pipeline filesystem cache exists.

The base64/zlib/JSON encoding asserted here was verified live against a real
dlt 1.28.1 sync (a dlt_native incremental resource, synced with `dlt.pipeline()`
directly against a scratch DuckDB file) on 2026-09-09 — not assumed from the
bug report. See PR description for the manual decode used to confirm this.
"""

import base64
import json
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from click.testing import CliRunner

from dango.cli.commands.source import (
    _decode_dlt_state,
    _iter_incremental_cursors,
    _resolve_pipeline_and_dataset_name,
    source_inspect_state,
)

_ANSI_RE = None  # not needed — CliRunner output is plain text (Rich falls back to no-color)


def _encode_state(state: dict) -> str:
    """Encode a dlt state dict the same way dlt does: JSON -> zlib -> base64."""
    return base64.b64encode(zlib.compress(json.dumps(state).encode())).decode()


def _make_mock_source(name: str = "test_src", dlt_native=None) -> MagicMock:
    """Create a mock DataSource, matching the pattern used in test_cli_sync.py."""
    mock_src = MagicMock()
    mock_src.name = name
    mock_src.type.value = "dlt_native" if dlt_native else "stripe"
    mock_src.enabled = True
    mock_src.dlt_native = dlt_native
    return mock_src


def _make_config(sources: list) -> MagicMock:
    mock_sources_cfg = MagicMock()
    mock_sources_cfg.sources = sources
    mock_sources_cfg.get_source.side_effect = lambda name: next(
        (s for s in sources if s.name == name), None
    )
    mock_config = MagicMock()
    mock_config.sources = mock_sources_cfg
    return mock_config


def _create_pipeline_state_table(
    duckdb_path: Path, dataset_name: str, state: dict | None, *, empty: bool = False
) -> None:
    """
    Create a real _dlt_pipeline_state table matching dlt's actual schema
    (verified live: version, engine_version, pipeline_name, state, created_at,
    version_hash, _dlt_load_id, _dlt_id).
    """
    conn = duckdb.connect(str(duckdb_path))
    try:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{dataset_name}"')
        conn.execute(f"""
            CREATE TABLE "{dataset_name}"._dlt_pipeline_state (
                version BIGINT,
                engine_version BIGINT,
                pipeline_name VARCHAR,
                state VARCHAR,
                created_at TIMESTAMPTZ,
                version_hash VARCHAR,
                _dlt_load_id VARCHAR,
                _dlt_id VARCHAR
            )
        """)
        if not empty:
            assert state is not None
            conn.execute(
                f'INSERT INTO "{dataset_name}"._dlt_pipeline_state VALUES '
                "(1, 4, 'test_pipeline', ?, now(), 'hash123', 'load1', 'id1')",
                [_encode_state(state)],
            )
    finally:
        conn.close()


_REAL_STATE = {
    "_state_version": 1,
    "_state_engine_version": 4,
    "default_schema_name": "test_src",
    "schema_names": ["test_src"],
    "pipeline_name": "test_pipeline",
    "dataset_name": "raw_test_src",
    "destination_type": "dlt.destinations.duckdb",
    "destination_name": None,
    "sources": {
        "test_src": {
            "resources": {
                "prices_daily": {
                    "incremental": {
                        "date": {
                            "initial_value": "2026-01-01",
                            "last_value": "2026-09-08",
                            "unique_hashes": ["abc123"],
                            "start_value": "2026-01-01",
                        }
                    }
                },
                "tickers": {},
            }
        }
    },
}


@pytest.mark.unit
class TestDecodeDltState:
    """Unit tests for the base64/zlib/JSON decode helper itself."""

    def test_decodes_real_encoding_roundtrip(self) -> None:
        encoded = _encode_state(_REAL_STATE)
        decoded = _decode_dlt_state(encoded)
        assert decoded == _REAL_STATE

    def test_invalid_base64_raises(self) -> None:
        import binascii

        with pytest.raises(binascii.Error):
            _decode_dlt_state("not valid base64!!!")


@pytest.mark.unit
class TestIterIncrementalCursors:
    def test_yields_cursor_for_incremental_resource(self) -> None:
        results = list(_iter_incremental_cursors(_REAL_STATE))
        cursor_rows = [r for r in results if r[2] is not None]
        assert len(cursor_rows) == 1
        dlt_source_name, resource_name, cursor_field, cursor_info = cursor_rows[0]
        assert dlt_source_name == "test_src"
        assert resource_name == "prices_daily"
        assert cursor_field == "date"
        assert cursor_info["last_value"] == "2026-09-08"

    def test_yields_no_cursor_marker_for_resource_without_incremental(self) -> None:
        results = list(_iter_incremental_cursors(_REAL_STATE))
        no_cursor_rows = [r for r in results if r[1] == "tickers"]
        assert len(no_cursor_rows) == 1
        assert no_cursor_rows[0][2] is None

    def test_empty_state_yields_nothing(self) -> None:
        assert list(_iter_incremental_cursors({})) == []


@pytest.mark.unit
class TestResolvePipelineAndDatasetName:
    def test_registry_source_uses_source_name(self) -> None:
        src = _make_mock_source(name="stripe_prod", dlt_native=None)
        pipeline_name, dataset_name = _resolve_pipeline_and_dataset_name(src)
        assert pipeline_name == "stripe_prod"
        assert dataset_name == "raw_stripe_prod"

    def test_dlt_native_source_defaults(self) -> None:
        dlt_native = MagicMock()
        dlt_native.pipeline_name = None
        dlt_native.dataset_name = None
        src = _make_mock_source(name="custom_src", dlt_native=dlt_native)
        pipeline_name, dataset_name = _resolve_pipeline_and_dataset_name(src)
        assert pipeline_name == "custom_src"
        assert dataset_name == "raw_custom_src"

    def test_dlt_native_source_explicit_override(self) -> None:
        dlt_native = MagicMock()
        dlt_native.pipeline_name = "my_pipeline"
        dlt_native.dataset_name = "my_dataset"
        src = _make_mock_source(name="custom_src", dlt_native=dlt_native)
        pipeline_name, dataset_name = _resolve_pipeline_and_dataset_name(src)
        assert pipeline_name == "my_pipeline"
        assert dataset_name == "my_dataset"


@pytest.mark.unit
class TestSourceInspectStateCommand:
    """End-to-end tests via CliRunner against a real (temp) DuckDB warehouse."""

    def _invoke(self, tmp_path: Path, source_name: str = "test_src"):
        runner = CliRunner()
        with patch("dango.cli.utils.require_project_context", return_value=tmp_path):
            result = runner.invoke(source_inspect_state, [source_name], obj={})
        return result

    def test_inspect_state_decodes_and_displays_last_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Isolate HOME so any incidental local-cache check doesn't touch the real ~/.dlt
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))

        src = _make_mock_source(name="test_src")
        mock_config = _make_config([src])

        (tmp_path / "data").mkdir()
        duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        _create_pipeline_state_table(duckdb_path, "raw_test_src", _REAL_STATE)

        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert "prices_daily" in result.output
        assert "2026-09-08" in result.output  # the last_value
        assert "date" in result.output  # cursor field name

    def test_inspect_state_handles_missing_table_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CSV-like source has no _dlt_pipeline_state table at all."""
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))

        src = _make_mock_source(name="csv_src")
        mock_config = _make_config([src])

        (tmp_path / "data").mkdir()
        duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        # Create the warehouse file with an unrelated schema, but no
        # _dlt_pipeline_state table in raw_csv_src at all.
        conn = duckdb.connect(str(duckdb_path))
        conn.execute('CREATE SCHEMA IF NOT EXISTS "raw_csv_src"')
        conn.execute('CREATE TABLE "raw_csv_src".some_table (id INTEGER)')
        conn.close()

        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path, "csv_src")

        assert result.exit_code == 0, result.output
        assert "No _dlt_pipeline_state table found" in result.output
        assert "Traceback" not in result.output

    def test_inspect_state_handles_no_state_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))

        src = _make_mock_source(name="test_src")
        mock_config = _make_config([src])

        (tmp_path / "data").mkdir()
        duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        _create_pipeline_state_table(duckdb_path, "raw_test_src", None, empty=True)

        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert "exists but has no rows" in result.output
        assert "Traceback" not in result.output

    def test_inspect_state_never_synced_no_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No warehouse.duckdb at all — never synced anything."""
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
        src = _make_mock_source(name="test_src")
        mock_config = _make_config([src])

        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert "never been synced" in result.output
        assert "Traceback" not in result.output

    def test_inspect_state_unknown_source_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
        mock_config = _make_config([_make_mock_source(name="other_src")])

        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path, "nonexistent")

        assert result.exit_code != 0
        assert "not found" in result.output

    @pytest.mark.parametrize("cache_exists", [True, False])
    def test_inspect_state_reports_local_cache_presence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache_exists: bool
    ) -> None:
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        src = _make_mock_source(name="test_src")
        mock_config = _make_config([src])

        if cache_exists:
            cache_dir = fake_home / ".dlt" / "pipelines" / "test_src"
            cache_dir.mkdir(parents=True)

        # No warehouse at all — we only care about the local-cache report here.
        with patch("dango.config.get_config", return_value=mock_config):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        if cache_exists:
            assert "exists" in result.output
        else:
            assert "not found" in result.output

    def test_inspect_state_never_writes_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Spy on every duckdb.connect() call the command makes and on every SQL
        statement it executes: assert every connection is opened read-only and
        no write/DDL statement (INSERT/UPDATE/DELETE/DROP/CREATE/ALTER) is ever
        issued.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))

        src = _make_mock_source(name="test_src")
        mock_config = _make_config([src])

        (tmp_path / "data").mkdir()
        duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        _create_pipeline_state_table(duckdb_path, "raw_test_src", _REAL_STATE)

        executed_sql: list[str] = []
        connect_configs: list[dict] = []
        real_connect = duckdb.connect

        class _SpyConnection:
            """Thin proxy: DuckDBPyConnection disallows attribute assignment
            (its `execute` slot is read-only), so wrap it instead of patching
            the method in place."""

            def __init__(self, real_conn) -> None:
                self._real_conn = real_conn

            def execute(self, sql, *a, **kw):
                executed_sql.append(sql)
                return self._real_conn.execute(sql, *a, **kw)

            def close(self):
                return self._real_conn.close()

        def spying_connect(*args, **kwargs):
            connect_configs.append(kwargs.get("config", {}))
            return _SpyConnection(real_connect(*args, **kwargs))

        with (
            patch("dango.config.get_config", return_value=mock_config),
            patch("duckdb.connect", side_effect=spying_connect),
        ):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert connect_configs, "expected at least one duckdb.connect() call"
        for cfg in connect_configs:
            assert cfg.get("access_mode") == "read_only", (
                f"Expected every connection to be read_only, got config={cfg}"
            )

        write_keywords = ("insert", "update", "delete", "drop", "create", "alter", "replace")
        for sql in executed_sql:
            lowered = sql.strip().lower()
            for keyword in write_keywords:
                assert not lowered.startswith(keyword), (
                    f"Command issued a write/DDL statement: {sql!r}"
                )
