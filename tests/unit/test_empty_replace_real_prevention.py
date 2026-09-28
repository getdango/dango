"""tests/unit/test_empty_replace_real_prevention.py

Regression coverage for 1.0.10-S13: empty-replace protection must actually
prevent the DuckDB write, not just signal failure after the write already
committed (see PLAN.md's "S13 finding" and dlt_runner.py's pre-load check,
added right after `pipeline.normalize()` in both `_run_dlt_native_source()`
and `_run_dlt_source()`).

`test_empty_replace_protection.py` covers this mechanism's mocked-dlt unit
tests already; this file adds the missing real, non-mocked coverage that
class was missing (per the "S13 finding": every existing test there mocked
dlt/DuckDB throughout and only asserted the error message + that
`_restore_dlt_state` was called, never that the destination data actually
survived). New file per the task's own line-count guidance -- the existing
file is already at 1800+ lines, well past the ~500-line threshold for
starting a new one.

Every scenario is mirrored for both `_run_dlt_native_source()` and
`_run_dlt_source()` -- the two parallel sync methods this fix had to be
applied to identically (a documented, recurring gotcha in this project).
"""

from __future__ import annotations

import importlib as _real_importlib
import types
from unittest.mock import patch

import dlt
import duckdb
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    """Redirect dlt's own pipeline working directory into tmp_path.

    dlt defaults to `~/.dlt/pipelines/{pipeline_name}` (see dlt_runner.py's
    `_backup_dlt_state` docstring) -- the real machine's home directory,
    which for a dev machine holds real, live Dango project state. These
    tests use throwaway pipeline names and never intend to touch that
    directory, so redirect dlt's own state via its documented env var.

    Note: Dango's own `_backup_dlt_state`/`_restore_dlt_state`/
    `_clear_local_pipeline_cache` hardcode `~/.dlt` regardless of this env
    var -- a separate, pre-existing inconsistency, out of this task's scope.
    With this fixture active they'll simply find no prior state to back up
    or restore for these never-before-used pipeline names (state_backup=
    None throughout), which doesn't affect any assertion in this file --
    that bookkeeping is orthogonal to the DuckDB data-survival behavior
    under test, and `_restore_dlt_state(None)` is already a documented no-op.
    """
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def _runner(tmp_path) -> DltPipelineRunner:
    return DltPipelineRunner(tmp_path)


def _native_config(name: str) -> DataSource:
    """A dlt_native DataSource. source_module/source_function are placeholder
    strings -- `importlib.import_module` is always mocked in these tests, so
    the real string value is never resolved against a real module."""
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
    """A generic verified-source DataSource. SourceType.CHESS is arbitrary --
    chosen only because it doesn't trigger any source-type-specific branches
    in _run_dlt_source (Facebook Ads, Zendesk, REST API). get_source_metadata
    and _load_dlt_source are always mocked, so the real chess source is never
    touched."""
    return DataSource(name=name, type=SourceType.CHESS)


def _make_resource(table_name, rows, write_disposition, primary_key):
    # A separate function call per resource -- each call gets its own local
    # scope/cell for `rows`, avoiding the classic late-binding closure bug
    # that bites when a generator closes over a shared loop variable. The
    # generator itself takes no parameters: dlt's config-spec reflection
    # rejects a mutable (list) default argument.
    resource_kwargs = {"name": table_name, "write_disposition": write_disposition}
    if primary_key:
        resource_kwargs["primary_key"] = primary_key

    @dlt.resource(**resource_kwargs)
    def _gen():
        yield from rows

    return _gen()


def _fixture_source(table_rows, write_disposition="replace", primary_key=None):
    """A real dlt source (not a mock) with one resource per table_rows entry.

    table_rows: {table_name: [row_dict, ...]}. Verified directly against the
    pinned dlt version (1.28.1) before use -- see the task's Experiments 2
    and this session's Experiment 5 (multi-table norm_info.row_counts).
    """

    @dlt.source(name="fixture_source")
    def _src():
        for table_name, rows in table_rows.items():
            yield _make_resource(table_name, rows, write_disposition, primary_key)

    return _src()


def _fixture_module(table_rows, write_disposition="replace", primary_key=None):
    """A fake importable module exposing `make_source()`, for
    _run_dlt_native_source's `importlib.import_module` mock target."""
    mod = types.ModuleType(f"s13_native_fixture_{id(table_rows)}")
    mod.make_source = lambda: _fixture_source(table_rows, write_disposition, primary_key)
    return mod


def _import_module_patch(fake_module):
    """Patch `dango.ingestion.dlt_runner.importlib.import_module` narrowly.

    `importlib` is a single shared module object -- patching its
    `import_module` attribute affects the ENTIRE process for the duration of
    the `with` block, not just this call site. A naive `return_value=`
    patch breaks anything else that dynamically imports during that window
    (confirmed directly: dlt.pipeline() construction triggers pendulum's
    locale loading via importlib.resources, which uses import_module
    internally and crashed when it always got our fake module back). This
    delegates to the REAL import_module for every name except the one
    fixed placeholder ("fixture_module") _native_config() always uses.
    """
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


@pytest.mark.unit
class TestNativeSourceRealPrevention:
    """_run_dlt_native_source(): real, non-mocked dlt + DuckDB coverage."""

    def test_never_calls_load_when_replace_table_would_go_empty(self, tmp_path):
        """Core behavioral change: when normalize()'s staged row_counts show 0
        for a replace-mode table that had existing rows, _load_with_lock()
        (and thus pipeline.load()) must never be called."""
        runner = _runner(tmp_path)
        config = _native_config("native_never_load")

        mod1 = _fixture_module({"mytable": [{"id": 1}, {"id": 2}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config)
        assert first["status"] == "success"

        mod2 = _fixture_module({"mytable": []})
        with (
            _import_module_patch(mod2),
            patch.object(runner, "_load_with_lock") as mock_load,
        ):
            second = runner._run_dlt_native_source(config)

        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        mock_load.assert_not_called()

    def test_preserves_real_data_end_to_end(self, tmp_path):
        """Acceptance criterion: a real, non-mocked DuckDB read confirms the
        destination table's prior data is unchanged after a blocked sync."""
        runner = _runner(tmp_path)
        config = _native_config("native_e2e_prevention")

        mod1 = _fixture_module({"mytable": [{"id": 1}, {"id": 2}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config)
        assert first["status"] == "success"

        rows_before = _read_table(runner.duckdb_path, "raw_native_e2e_prevention", "mytable")
        assert rows_before  # sanity: the first sync actually landed real data

        mod2 = _fixture_module({"mytable": []})
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config)

        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "would truncate" in second["error"]

        rows_after = _read_table(runner.duckdb_path, "raw_native_e2e_prevention", "mytable")
        assert rows_after == rows_before

    def test_abandoned_package_does_not_contaminate_next_sync(self, tmp_path):
        """Regression test for the task's Experiment 3: a blocked sync must
        call pipeline.drop_pending_packages() so no stale pending package is
        left to be silently swept into a later sync's load()."""
        runner = _runner(tmp_path)
        config = _native_config("native_no_contamination")

        mod1 = _fixture_module({"mytable": [{"id": 1}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config)
        assert first["status"] == "success"

        mod2 = _fixture_module({"mytable": []})
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config)
        assert second["status"] == "failed"

        # Verify against the real, on-disk dlt pipeline state (a fresh
        # pipeline object, mirroring exactly how the next `dango sync`
        # invocation would see it) that nothing is left pending.
        check_pipeline = dlt.pipeline(
            pipeline_name="native_no_contamination",
            destination=dlt.destinations.duckdb(credentials=str(runner.duckdb_path)),
            dataset_name="raw_native_no_contamination",
        )
        assert check_pipeline.list_normalized_load_packages() == []
        del check_pipeline

        mod3 = _fixture_module({"mytable": [{"id": 2}, {"id": 3}]})
        with _import_module_patch(mod3):
            third = runner._run_dlt_native_source(config)
        assert third["status"] == "success"

        rows = _read_table(runner.duckdb_path, "raw_native_no_contamination", "mytable")
        assert sorted(r[0] for r in rows) == [2, 3]

    def test_allow_policy_still_loads_normally(self, tmp_path):
        """empty_sync_policy="allow" (allow_empty_replace=True) is unaffected:
        load still proceeds and the table legitimately ends up empty."""
        runner = _runner(tmp_path)
        config = _native_config("native_allow_policy")

        mod1 = _fixture_module({"mytable": [{"id": 1}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config)
        assert first["status"] == "success"

        mod2 = _fixture_module({"mytable": []})
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, allow_empty_replace=True)

        assert second["status"] == "success"
        rows = _read_table(runner.duckdb_path, "raw_native_allow_policy", "mytable")
        assert rows == []

    def test_per_table_partial_empty_still_blocks(self, tmp_path):
        """A multi-resource source where one table would go to 0 and another
        has real data: the whole sync still blocks (all-or-nothing, matching
        the existing post-load per-table check's semantics -- this task only
        moves *when* the fully-empty case is caught, not this behavior)."""
        runner = _runner(tmp_path)
        config = _native_config("native_partial_empty")

        mod1 = _fixture_module({"table_a": [{"id": 1}], "table_b": [{"id": 10}]})
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config)
        assert first["status"] == "success"

        rows_a_before = _read_table(runner.duckdb_path, "raw_native_partial_empty", "table_a")
        rows_b_before = _read_table(runner.duckdb_path, "raw_native_partial_empty", "table_b")

        # table_a stays populated, table_b goes empty -> whole sync blocks.
        mod2 = _fixture_module({"table_a": [{"id": 2}], "table_b": []})
        with (
            _import_module_patch(mod2),
            patch.object(runner, "_load_with_lock") as mock_load,
        ):
            second = runner._run_dlt_native_source(config)

        assert second["status"] == "failed"
        assert "table_b" in second["error"]
        assert "table_a" not in second["error"]
        mock_load.assert_not_called()

        rows_a_after = _read_table(runner.duckdb_path, "raw_native_partial_empty", "table_a")
        rows_b_after = _read_table(runner.duckdb_path, "raw_native_partial_empty", "table_b")
        assert rows_a_after == rows_a_before
        assert rows_b_after == rows_b_before

    def test_full_refresh_merge_mode_unaffected(self, tmp_path):
        """A merge/append source run with --full-refresh behaves identically
        to before this task: the new pre-load check is gated on
        uses_replace_mode only, so it never fires for merge mode -- this
        falls through unchanged to the existing post-load check, which
        already gates on `full_refresh or uses_replace_mode`."""
        runner = _runner(tmp_path)
        config = _native_config("native_merge_full_refresh")

        mod1 = _fixture_module(
            {"mytable": [{"id": 1}]}, write_disposition="merge", primary_key="id"
        )
        with _import_module_patch(mod1):
            first = runner._run_dlt_native_source(config, full_refresh=True)
        assert first["status"] == "success"
        assert first["uses_replace_mode"] is False

        mod2 = _fixture_module({"mytable": []}, write_disposition="merge", primary_key="id")
        with _import_module_patch(mod2):
            second = runner._run_dlt_native_source(config, full_refresh=True)

        # Pre-existing behavior (unchanged by this task): full_refresh + 0
        # rows + previously-existing data still fails via the OLD post-load
        # check, since that check gates on `full_refresh or uses_replace_mode`.
        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "existing 1 rows preserved" in second["error"]


@pytest.mark.unit
class TestDltSourceRealPrevention:
    """_run_dlt_source(): real, non-mocked dlt + DuckDB coverage.

    get_source_metadata/_load_dlt_source are mocked to bypass Dango's
    registry/dynamic-import wiring (the same wiring TestCoreProtection's
    existing mocked tests bypass) -- everything downstream (dlt.pipeline,
    extract/normalize/load, real DuckDB writes) runs for real.
    """

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

    def test_never_calls_load_when_replace_table_would_go_empty(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_never_load")

        first = self._run(runner, config, _fixture_source({"mytable": [{"id": 1}, {"id": 2}]}))
        assert first["status"] == "success"

        with patch.object(runner, "_load_with_lock") as mock_load:
            second = self._run(runner, config, _fixture_source({"mytable": []}))

        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        mock_load.assert_not_called()

    def test_preserves_real_data_end_to_end(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_e2e_prevention")

        first = self._run(runner, config, _fixture_source({"mytable": [{"id": 1}, {"id": 2}]}))
        assert first["status"] == "success"

        rows_before = _read_table(runner.duckdb_path, "raw_dltsource_e2e_prevention", "mytable")
        assert rows_before

        second = self._run(runner, config, _fixture_source({"mytable": []}))

        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "would truncate" in second["error"]

        rows_after = _read_table(runner.duckdb_path, "raw_dltsource_e2e_prevention", "mytable")
        assert rows_after == rows_before

    def test_abandoned_package_does_not_contaminate_next_sync(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_no_contamination")

        first = self._run(runner, config, _fixture_source({"mytable": [{"id": 1}]}))
        assert first["status"] == "success"

        second = self._run(runner, config, _fixture_source({"mytable": []}))
        assert second["status"] == "failed"

        check_pipeline = dlt.pipeline(
            pipeline_name="dltsource_no_contamination",
            destination=dlt.destinations.duckdb(credentials=str(runner.duckdb_path)),
            dataset_name="raw_dltsource_no_contamination",
        )
        assert check_pipeline.list_normalized_load_packages() == []
        del check_pipeline

        third = self._run(runner, config, _fixture_source({"mytable": [{"id": 2}, {"id": 3}]}))
        assert third["status"] == "success"

        rows = _read_table(runner.duckdb_path, "raw_dltsource_no_contamination", "mytable")
        assert sorted(r[0] for r in rows) == [2, 3]

    def test_allow_policy_still_loads_normally(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_allow_policy")

        first = self._run(runner, config, _fixture_source({"mytable": [{"id": 1}]}))
        assert first["status"] == "success"

        second = self._run(
            runner, config, _fixture_source({"mytable": []}), allow_empty_replace=True
        )
        assert second["status"] == "success"

        rows = _read_table(runner.duckdb_path, "raw_dltsource_allow_policy", "mytable")
        assert rows == []

    def test_per_table_partial_empty_still_blocks(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_partial_empty")

        first = self._run(
            runner,
            config,
            _fixture_source({"table_a": [{"id": 1}], "table_b": [{"id": 10}]}),
        )
        assert first["status"] == "success"

        rows_a_before = _read_table(runner.duckdb_path, "raw_dltsource_partial_empty", "table_a")
        rows_b_before = _read_table(runner.duckdb_path, "raw_dltsource_partial_empty", "table_b")

        with patch.object(runner, "_load_with_lock") as mock_load:
            second = self._run(
                runner,
                config,
                _fixture_source({"table_a": [{"id": 2}], "table_b": []}),
            )

        assert second["status"] == "failed"
        assert "table_b" in second["error"]
        assert "table_a" not in second["error"]
        mock_load.assert_not_called()

        rows_a_after = _read_table(runner.duckdb_path, "raw_dltsource_partial_empty", "table_a")
        rows_b_after = _read_table(runner.duckdb_path, "raw_dltsource_partial_empty", "table_b")
        assert rows_a_after == rows_a_before
        assert rows_b_after == rows_b_before

    def test_full_refresh_merge_mode_unaffected(self, tmp_path):
        runner = _runner(tmp_path)
        config = _dltsource_config("dltsource_merge_full_refresh")

        first = self._run(
            runner,
            config,
            _fixture_source({"mytable": [{"id": 1}]}, write_disposition="merge", primary_key="id"),
            full_refresh=True,
        )
        assert first["status"] == "success"
        assert first["uses_replace_mode"] is False

        second = self._run(
            runner,
            config,
            _fixture_source({"mytable": []}, write_disposition="merge", primary_key="id"),
            full_refresh=True,
        )

        assert second["status"] == "failed"
        assert second["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "existing 1 rows preserved" in second["error"]
