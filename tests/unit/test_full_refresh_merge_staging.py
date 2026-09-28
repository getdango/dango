"""tests/unit/test_full_refresh_merge_staging.py

Regression coverage for 1.0.10-S15: `--full-refresh` on merge/append sources stages
the reload into a separate pipeline and swaps it into the real destination only once
known-good, via `_full_refresh_via_staging()` (called identically by both
`_run_dlt_native_source()` and `_run_dlt_source()`). Real, non-mocked dlt + DuckDB.
"""

from __future__ import annotations

import importlib as _real_importlib
import types
from pathlib import Path
from unittest.mock import patch

import dlt
import duckdb
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    """Redirect dlt's pipeline working directory into tmp_path (see the identical
    fixture in test_empty_replace_real_prevention.py) -- never-before-used pipeline
    names mean nothing to back up/restore, orthogonal to this file's assertions."""
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Skip _run_extract_with_retry's exponential backoff wait -- doesn't change
    retry count/control flow, just the real wall-clock delay in failure tests."""
    monkeypatch.setattr("dango.ingestion.dlt_runner.time.sleep", lambda _seconds: None)


def _runner(tmp_path) -> DltPipelineRunner:
    return DltPipelineRunner(tmp_path)


def _native_config(name: str) -> DataSource:
    """A dlt_native DataSource -- source_module/source_function are placeholder
    strings, always resolved via the mocked importlib.import_module below."""
    return DataSource(
        name=name,
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="fixture_module",
            source_function="make_source",
            function_kwargs={},
        ),
    )


def _dltsource_config(name: str) -> DataSource:
    """A generic verified-source DataSource. SourceType.CHESS is arbitrary -- it
    doesn't trigger any source-type-specific branches in _run_dlt_source."""
    return DataSource(name=name, type=SourceType.CHESS)


def _make_resource(table_name, rows, write_disposition, primary_key, fail_after=None):
    # Separate call per resource -- own closure cell per `rows`/`fail_after`.
    resource_kwargs = {"name": table_name, "write_disposition": write_disposition}
    if primary_key:
        resource_kwargs["primary_key"] = primary_key

    @dlt.resource(**resource_kwargs)
    def _gen():
        count = 0
        for row in rows:
            if fail_after is not None and count == fail_after:
                raise RuntimeError("simulated failure during staging extraction")
            yield row
            count += 1

    return _gen()


def _fixture_source(table_rows, write_disposition="merge", primary_key="id", fail_table=None):
    """A real dlt source with one resource per table_rows entry. `fail_table`, if
    given, is a (table_name, fail_after_n_rows) pair that raises partway through."""

    @dlt.source(name="fixture_source")
    def _src():
        for table_name, rows in table_rows.items():
            fail_after = None
            if fail_table is not None and fail_table[0] == table_name:
                fail_after = fail_table[1]
            yield _make_resource(table_name, rows, write_disposition, primary_key, fail_after)

    return _src()


def _fixture_module(table_rows, write_disposition="merge", primary_key="id", fail_table=None):
    """A fake importable module exposing `make_source()`, for
    _run_dlt_native_source's `importlib.import_module` mock target."""
    mod = types.ModuleType(f"s15_native_fixture_{id(table_rows)}")
    mod.make_source = lambda: _fixture_source(
        table_rows, write_disposition, primary_key, fail_table
    )
    return mod


def _import_module_patch(fake_module):
    """Patch import_module narrowly -- a naive return_value= patch breaks
    pendulum's own importlib usage during dlt.pipeline() construction."""
    real_import_module = _real_importlib.import_module

    def _side_effect(name, *args, **kwargs):
        if name == "fixture_module":
            return fake_module
        return real_import_module(name, *args, **kwargs)

    return patch("dango.ingestion.dlt_runner.importlib.import_module", side_effect=_side_effect)


def _read_table(duckdb_path, schema, table):
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        return con.execute(f'SELECT * FROM "{schema}"."{table}" ORDER BY id').fetchall()
    finally:
        con.close()


def _schema_exists(duckdb_path, schema_name: str) -> bool:
    # No warehouse file yet (before a source's first-ever sync) != schema missing.
    if not Path(duckdb_path).exists():
        return False
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        result = con.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = ?",
            [schema_name],
        ).fetchone()
        return result is not None
    finally:
        con.close()


class _FailOnTableConnection:
    """Real duckdb connection proxy: delegates to a genuine connection, except
    raises on CREATE TABLE for one table -- a real mid-swap-loop failure."""

    def __init__(self, real_conn, fail_table: str):
        self._real_conn = real_conn
        self._fail_table = fail_table

    def execute(self, sql, *args, **kwargs):
        if "CREATE TABLE" in sql and f'"{self._fail_table}"' in sql and " AS SELECT" in sql:
            raise RuntimeError(f"simulated failure swapping in {self._fail_table}")
        return self._real_conn.execute(sql, *args, **kwargs)

    def close(self):
        self._real_conn.close()


def _patch_swap_failure(fail_table: str):
    """Patch _connect_with_lock_retry so only the swap step's own connection
    (operation="fullrefresh-staging-swap") fails creating `fail_table` --
    every other call stays real."""
    import dango.ingestion.dlt_runner as _dlt_runner_module

    real_connect = _dlt_runner_module._connect_with_lock_retry

    def _side_effect(duckdb_path, source_name, operation, project_root=None):
        real_conn = real_connect(duckdb_path, source_name, operation, project_root=project_root)
        if operation == "fullrefresh-staging-swap":
            return _FailOnTableConnection(real_conn, fail_table)
        return real_conn

    return patch("dango.ingestion.dlt_runner._connect_with_lock_retry", side_effect=_side_effect)


@pytest.mark.unit
class TestNativeSourceFullRefreshStaging:
    """_run_dlt_native_source(): real, non-mocked dlt + DuckDB coverage of the
    stage-then-swap full-refresh path."""

    def test_full_refresh_purges_deleted_rows(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_purge")

        mod1 = _fixture_module(
            {"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]}
        )
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"

        # id=1 and id=3 absent (deleted upstream), id=2 updated, id=4 new.
        mod2 = _fixture_module({"mytable": [{"id": 2, "v": "b-updated"}, {"id": 4, "v": "d-new"}]})
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, full_refresh=True)

        assert second["status"] == "success"
        rows = _read_table(runner.duckdb_path, "raw_native_purge", "mytable")
        data = [(r[0], r[1]) for r in rows]
        assert data == [(2, "b-updated"), (4, "d-new")]

    def test_full_refresh_failure_leaves_real_destination_untouched(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_fail")

        mod1 = _fixture_module({"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"
        rows_before = _read_table(runner.duckdb_path, "raw_native_fail", "mytable")
        assert rows_before

        mod2 = _fixture_module(
            {"mytable": [{"id": 2, "v": "b2"}, {"id": 3, "v": "c"}]},
            fail_table=("mytable", 1),
        )
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, full_refresh=True)

        assert second["status"] == "failed"
        assert second["rows_loaded"] == 0

        rows_after = _read_table(runner.duckdb_path, "raw_native_fail", "mytable")
        assert rows_after == rows_before

        assert not _schema_exists(runner.duckdb_path, "raw_native_fail_fullrefresh_staging")
        assert not _schema_exists(runner.duckdb_path, "raw_native_fail_fullrefresh_staging_staging")

    def test_full_refresh_multi_table_source_swaps_all_tables(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_multitable")

        mod1 = _fixture_module(
            {"t1": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], "t2": [{"id": 10, "v": "x"}]}
        )
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"

        mod2 = _fixture_module(
            {
                "t1": [{"id": 2, "v": "b-updated"}, {"id": 3, "v": "c-new"}],
                "t2": [{"id": 11, "v": "y-new"}],
            }
        )
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, full_refresh=True)
        assert second["status"] == "success"

        t1_rows = _read_table(runner.duckdb_path, "raw_native_multitable", "t1")
        t2_rows = _read_table(runner.duckdb_path, "raw_native_multitable", "t2")
        assert [(r[0], r[1]) for r in t1_rows] == [(2, "b-updated"), (3, "c-new")]
        assert [(r[0], r[1]) for r in t2_rows] == [(11, "y-new")]

    def test_full_refresh_swap_failure_leaves_all_real_tables_untouched(self, tmp_path):
        """Staging load succeeds, swap loop fails partway (table_b's CREATE) --
        without transaction wrapping, table_a's drop+recreate would already be
        committed. Assert every real table is byte-for-byte unchanged."""
        runner = _runner(tmp_path)
        config = _native_config("native_swap_fail")

        mod1 = _fixture_module(
            {"table_a": [{"id": 1, "v": "a"}], "table_b": [{"id": 10, "v": "x"}]}
        )
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"

        rows_a_before = _read_table(runner.duckdb_path, "raw_native_swap_fail", "table_a")
        rows_b_before = _read_table(runner.duckdb_path, "raw_native_swap_fail", "table_b")

        mod2 = _fixture_module(
            {"table_a": [{"id": 2, "v": "a-updated"}], "table_b": [{"id": 20, "v": "y"}]}
        )
        with _import_module_patch(mod2), _patch_swap_failure("table_b"):
            second = runner._run_dlt_native_source(config, full_refresh=True)

        assert second["status"] == "failed"

        rows_a_after = _read_table(runner.duckdb_path, "raw_native_swap_fail", "table_a")
        rows_b_after = _read_table(runner.duckdb_path, "raw_native_swap_fail", "table_b")
        assert rows_a_after == rows_a_before
        assert rows_b_after == rows_b_before

    def test_full_refresh_cleans_up_dlt_internal_staging_schema(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_cleanup")

        mod1 = _fixture_module({"mytable": [{"id": 1, "v": "a"}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"

        mod2 = _fixture_module({"mytable": [{"id": 2, "v": "b"}]})
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, full_refresh=True)
        assert second["status"] == "success"

        assert not _schema_exists(runner.duckdb_path, "raw_native_cleanup_fullrefresh_staging")
        assert not _schema_exists(
            runner.duckdb_path, "raw_native_cleanup_fullrefresh_staging_staging"
        )

    def test_full_refresh_first_sync_ever_still_works(self, tmp_path):
        runner = _runner(tmp_path)
        config = _native_config("native_first_ever")

        assert not _schema_exists(runner.duckdb_path, "raw_native_first_ever")

        mod = _fixture_module({"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]})
        with _import_module_patch(mod):
            result = runner._run_dlt_native_source(config, full_refresh=True)

        assert result["status"] == "success"
        rows = _read_table(runner.duckdb_path, "raw_native_first_ever", "mytable")
        assert [(r[0], r[1]) for r in rows] == [(1, "a"), (2, "b")]


@pytest.mark.unit
class TestDltSourceFullRefreshStaging:
    """_run_dlt_source(): real, non-mocked dlt + DuckDB coverage. Only
    get_source_metadata/_load_dlt_source are mocked (registry/dynamic-import
    wiring); everything downstream runs for real."""

    @staticmethod
    def _run(runner, config, source, **kwargs):
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

    def test_full_refresh_purges_deleted_rows(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_purge")

        first = self._run(
            runner,
            config,
            _fixture_source(
                {"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]}
            ),
            full_refresh=True,
        )
        assert first["status"] == "success"

        second = self._run(
            runner,
            config,
            _fixture_source({"mytable": [{"id": 2, "v": "b-updated"}, {"id": 4, "v": "d-new"}]}),
            full_refresh=True,
        )
        assert second["status"] == "success"

        rows = _read_table(runner.duckdb_path, "raw_dltsource_purge", "mytable")
        data = [(r[0], r[1]) for r in rows]
        assert data == [(2, "b-updated"), (4, "d-new")]

    def test_full_refresh_failure_leaves_real_destination_untouched(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_fail")

        first = self._run(
            runner,
            config,
            _fixture_source({"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}),
            full_refresh=True,
        )
        assert first["status"] == "success"
        rows_before = _read_table(runner.duckdb_path, "raw_dltsource_fail", "mytable")
        assert rows_before

        second = self._run(
            runner,
            config,
            _fixture_source(
                {"mytable": [{"id": 2, "v": "b2"}, {"id": 3, "v": "c"}]},
                fail_table=("mytable", 1),
            ),
            full_refresh=True,
        )

        assert second["status"] == "failed"
        assert second["rows_loaded"] == 0

        rows_after = _read_table(runner.duckdb_path, "raw_dltsource_fail", "mytable")
        assert rows_after == rows_before

        assert not _schema_exists(runner.duckdb_path, "raw_dltsource_fail_fullrefresh_staging")
        assert not _schema_exists(
            runner.duckdb_path, "raw_dltsource_fail_fullrefresh_staging_staging"
        )

    def test_full_refresh_multi_table_source_swaps_all_tables(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_multitable")

        first = self._run(
            runner,
            config,
            _fixture_source(
                {"t1": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}], "t2": [{"id": 10, "v": "x"}]}
            ),
            full_refresh=True,
        )
        assert first["status"] == "success"

        second = self._run(
            runner,
            config,
            _fixture_source(
                {
                    "t1": [{"id": 2, "v": "b-updated"}, {"id": 3, "v": "c-new"}],
                    "t2": [{"id": 11, "v": "y-new"}],
                }
            ),
            full_refresh=True,
        )
        assert second["status"] == "success"

        t1_rows = _read_table(runner.duckdb_path, "raw_dltsource_multitable", "t1")
        t2_rows = _read_table(runner.duckdb_path, "raw_dltsource_multitable", "t2")
        assert [(r[0], r[1]) for r in t1_rows] == [(2, "b-updated"), (3, "c-new")]
        assert [(r[0], r[1]) for r in t2_rows] == [(11, "y-new")]

    def test_full_refresh_swap_failure_leaves_all_real_tables_untouched(self, tmp_path):
        """See the identical-intent native test above for the full rationale --
        mirrored here for _run_dlt_source()."""
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_swap_fail")

        first = self._run(
            runner,
            config,
            _fixture_source({"table_a": [{"id": 1, "v": "a"}], "table_b": [{"id": 10, "v": "x"}]}),
            full_refresh=True,
        )
        assert first["status"] == "success"

        rows_a_before = _read_table(runner.duckdb_path, "raw_dltsource_swap_fail", "table_a")
        rows_b_before = _read_table(runner.duckdb_path, "raw_dltsource_swap_fail", "table_b")

        with _patch_swap_failure("table_b"):
            second = self._run(
                runner,
                config,
                _fixture_source(
                    {"table_a": [{"id": 2, "v": "a2"}], "table_b": [{"id": 20, "v": "y"}]}
                ),
                full_refresh=True,
            )

        assert second["status"] == "failed"

        rows_a_after = _read_table(runner.duckdb_path, "raw_dltsource_swap_fail", "table_a")
        rows_b_after = _read_table(runner.duckdb_path, "raw_dltsource_swap_fail", "table_b")
        assert rows_a_after == rows_a_before
        assert rows_b_after == rows_b_before

    def test_full_refresh_cleans_up_dlt_internal_staging_schema(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_cleanup")

        first = self._run(
            runner, config, _fixture_source({"mytable": [{"id": 1, "v": "a"}]}), full_refresh=True
        )
        assert first["status"] == "success"

        second = self._run(
            runner, config, _fixture_source({"mytable": [{"id": 2, "v": "b"}]}), full_refresh=True
        )
        assert second["status"] == "success"

        assert not _schema_exists(runner.duckdb_path, "raw_dltsource_cleanup_fullrefresh_staging")
        assert not _schema_exists(
            runner.duckdb_path, "raw_dltsource_cleanup_fullrefresh_staging_staging"
        )

    def test_full_refresh_first_sync_ever_still_works(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_first_ever")

        assert not _schema_exists(runner.duckdb_path, "raw_dltsource_first_ever")

        result = self._run(
            runner,
            config,
            _fixture_source({"mytable": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}),
            full_refresh=True,
        )

        assert result["status"] == "success"
        rows = _read_table(runner.duckdb_path, "raw_dltsource_first_ever", "mytable")
        assert [(r[0], r[1]) for r in rows] == [(1, "a"), (2, "b")]
