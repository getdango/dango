"""tests/unit/test_full_refresh_merge_staging_replace_mode.py

Regression coverage for 1.0.10-S15: confirms replace-mode `--full-refresh` is
byte-for-byte unchanged -- still the untouched `else:` branch (pipeline.drop() +
recreate) in both `_run_dlt_native_source()` and `_run_dlt_source()`, never the new
`_full_refresh_via_staging()` helper. Split out of test_full_refresh_merge_staging.py
(which covers the staging mechanism itself) to keep both files under the
file-size-check line limit -- same pattern as test_dlt_runner_full_refresh.py /
test_dlt_runner_local_cache.py's split. Real, non-mocked dlt + DuckDB throughout.
"""

from __future__ import annotations

import importlib as _real_importlib
from pathlib import Path
from unittest.mock import patch

import dlt
import duckdb
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    """See the identical fixture in test_full_refresh_merge_staging.py."""
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def _runner(tmp_path) -> DltPipelineRunner:
    return DltPipelineRunner(tmp_path)


def _native_config(name: str) -> DataSource:
    return DataSource(
        name=name,
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="fixture_module", source_function="make_source", function_kwargs={}
        ),
    )


def _dltsource_config(name: str) -> DataSource:
    return DataSource(name=name, type=SourceType.CHESS)


def _make_resource(table_name, rows, write_disposition):
    # Separate function call per resource -- each gets its own closure cell for
    # `rows`, avoiding both the late-binding bug and dlt's config-spec reflection
    # rejecting a mutable (list) default argument.
    @dlt.resource(name=table_name, write_disposition=write_disposition)
    def _gen():
        yield from rows

    return _gen()


def _fixture_source(table_rows, write_disposition="merge"):
    @dlt.source(name="fixture_source")
    def _src():
        for table_name, rows in table_rows.items():
            yield _make_resource(table_name, rows, write_disposition)

    return _src()


def _fixture_module(table_rows, write_disposition="merge"):
    import types

    mod = types.ModuleType(f"s15_replace_fixture_{id(table_rows)}")
    mod.make_source = lambda: _fixture_source(table_rows, write_disposition)
    return mod


def _import_module_patch(fake_module):
    """See the identical helper in test_full_refresh_merge_staging.py."""
    real_import_module = _real_importlib.import_module

    def _side_effect(name, *args, **kwargs):
        if name == "fixture_module":
            return fake_module
        return real_import_module(name, *args, **kwargs)

    return patch("dango.ingestion.dlt_runner.importlib.import_module", side_effect=_side_effect)


def _run_dltsource(runner, config, source, **kwargs):
    with (
        patch(
            "dango.ingestion.dlt_runner.get_source_metadata",
            return_value={"dlt_package": "fixture", "dlt_function": "fixture_func"},
        ),
        patch.object(runner, "_build_source_config", return_value={}),
        patch.object(runner, "_load_dlt_source", return_value=source),
        patch.object(runner, "_check_oauth_token_expiry", return_value=None),
        patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, k: k),
    ):
        return runner._run_dlt_source(config, **kwargs)


def _read_table(duckdb_path, schema, table):
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return con.execute(f'SELECT * FROM "{schema}"."{table}" ORDER BY id').fetchall()
    finally:
        con.close()


def _schema_exists(duckdb_path, schema_name: str) -> bool:
    if not Path(duckdb_path).exists():
        return False
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        result = con.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = ?", [schema_name]
        ).fetchone()
        return result is not None
    finally:
        con.close()


@pytest.mark.unit
class TestReplaceModeUnaffected:
    """Confirm replace-mode --full-refresh is unchanged: still the untouched
    `else:` branch, never the new staging helper. Mirrors both methods."""

    def test_native_replace_mode_full_refresh_unaffected(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_replace")

        mod1 = _fixture_module(
            {"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}, write_disposition="replace"
        )
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"
        assert first["uses_replace_mode"] is True

        mod2 = _fixture_module({"mytable": [{"id": 5, "v": "z"}]}, write_disposition="replace")
        with (
            _import_module_patch(mod2),
            patch(
                "dango.ingestion.dlt_runner.DltPipelineRunner._full_refresh_via_staging"
            ) as mock_staging,
        ):
            second = runner._run_dlt_native_source(config, full_refresh=True)

        assert second["status"] == "success"
        mock_staging.assert_not_called()
        rows = _read_table(runner.duckdb_path, "raw_native_replace", "mytable")
        assert [(r[0], r[1]) for r in rows] == [(5, "z")]
        assert not _schema_exists(runner.duckdb_path, "raw_native_replace_fullrefresh_staging")

    def test_dltsource_replace_mode_full_refresh_unaffected(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_replace")

        first = _run_dltsource(
            runner,
            config,
            _fixture_source(
                {"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}, write_disposition="replace"
            ),
            full_refresh=True,
        )
        assert first["status"] == "success"
        assert first["uses_replace_mode"] is True

        with patch(
            "dango.ingestion.dlt_runner.DltPipelineRunner._full_refresh_via_staging"
        ) as mock_staging:
            second = _run_dltsource(
                runner,
                config,
                _fixture_source({"mytable": [{"id": 5, "v": "z"}]}, write_disposition="replace"),
                full_refresh=True,
            )

        assert second["status"] == "success"
        mock_staging.assert_not_called()
        rows = _read_table(runner.duckdb_path, "raw_dltsource_replace", "mytable")
        assert [(r[0], r[1]) for r in rows] == [(5, "z")]
        assert not _schema_exists(runner.duckdb_path, "raw_dltsource_replace_fullrefresh_staging")
