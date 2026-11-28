"""tests/unit/test_dlt_state_removed_with_source.py

`dango source remove` and `dango db clean` end the local dlt pipeline state (the incremental cursor) of
the source whose data is gone, so a source re-added under the same name loads every row again. Real dlt
pipelines against a local DuckDB under a throwaway HOME, both sync methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.ingestion.sources.setup_lifecycle import remove_source
from tests.factories.dlt_state_projects import (
    db_clean,
    drop_schema,
    entry,
    failed_load,
    ids,
    isolate_home,
    pipeline_dirs,
    project,
    run_sync,
    write_sources,
)
from tests.factories.duckdb_holder import hold_warehouse
from tests.factories.empty_guard_runs import METHODS

pytestmark = pytest.mark.unit

NAME = "ev"


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def synced(tmp_path: Path, method: str, rows: int = 5) -> Path:
    root = project(tmp_path, "p1", entry(NAME))
    assert run_sync(root, method, NAME, rows)["status"] == "success"
    assert ids(root, NAME) == upto(rows)
    return root


@pytest.mark.parametrize("method", METHODS)
class TestRemoveThenCleanThenReAdd:
    def test_every_row_loads_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        root = synced(tmp_path, method)
        remove_source(root, NAME, force=True)
        assert db_clean(root, monkeypatch).exit_code == 0
        write_sources(root, entry(NAME))

        assert run_sync(root, method, NAME, 5)["status"] == "success"
        assert ids(root, NAME) == upto(5)

    def test_after_a_staged_refresh_the_cursor_dies_with_the_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        root = synced(tmp_path, method)
        assert run_sync(root, method, NAME, 8, full_refresh=True)["status"] == "success"
        remove_source(root, NAME, force=True)
        db_clean(root, monkeypatch)
        write_sources(root, entry(NAME))

        assert run_sync(root, method, NAME, 8)["status"] == "success"
        assert ids(root, NAME) == upto(8)

    def test_a_kept_package_of_a_failed_load_is_dropped_with_the_cleaned_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        root = synced(tmp_path, method)
        failed_load(root, method, NAME, 8)
        remove_source(root, NAME, force=True)
        db_clean(root, monkeypatch)
        write_sources(root, entry(NAME))

        assert run_sync(root, method, NAME, 10)["status"] == "success"
        assert ids(root, NAME) == upto(10)


@pytest.mark.parametrize("method", METHODS)
class TestRemoveAloneKeepsTheCursorWhileDataRemains:
    """`source remove` never deletes warehouse data, so a source re-added before `db clean` must
    continue from its cursor (a dropped cursor would load every row a second time)."""

    def test_no_row_is_loaded_twice(self, tmp_path: Path, method: str) -> None:
        root = synced(tmp_path, method)
        remove_source(root, NAME, force=True)
        assert NAME in pipeline_dirs()
        write_sources(root, entry(NAME))

        assert run_sync(root, method, NAME, 8)["status"] == "success"
        assert ids(root, NAME) == upto(8)

    def test_a_kept_package_still_loads_once(self, tmp_path: Path, method: str) -> None:
        root = synced(tmp_path, method)
        failed_load(root, method, NAME, 8)
        remove_source(root, NAME, force=True)
        write_sources(root, entry(NAME))

        assert run_sync(root, method, NAME, 10)["status"] == "success"
        assert ids(root, NAME) == upto(10)

    def test_dry_run_changes_nothing(self, tmp_path: Path, method: str) -> None:
        root = synced(tmp_path, method)
        drop_schema(root, f"raw_{NAME}")
        remove_source(root, NAME, dry_run=True)
        assert NAME in pipeline_dirs()


@pytest.mark.parametrize("method", METHODS)
class TestRemoveAfterTheDataIsGone:
    def test_the_pipeline_dir_goes_with_the_source(self, tmp_path: Path, method: str) -> None:
        root = synced(tmp_path, method)
        drop_schema(root, f"raw_{NAME}")

        result = remove_source(root, NAME, force=True)
        assert NAME not in pipeline_dirs()
        assert result.pipeline_state_cleared is True
        write_sources(root, entry(NAME))
        assert run_sync(root, method, NAME, 5)["status"] == "success"
        assert ids(root, NAME) == upto(5)

    def test_a_missing_warehouse_counts_as_no_data(self, tmp_path: Path, method: str) -> None:
        root = synced(tmp_path, method)
        (root / "data" / "warehouse.duckdb").unlink()
        remove_source(root, NAME, force=True)
        assert NAME not in pipeline_dirs()


class TestWhatRemoveLeavesAlone:
    def test_a_warehouse_that_cannot_be_read_keeps_the_cursor(self, tmp_path: Path) -> None:
        root = synced(tmp_path, "native")
        with hold_warehouse(root / "data" / "warehouse.duckdb"):
            remove_source(root, NAME, force=True)
        assert NAME in pipeline_dirs()

    def test_another_source_with_the_same_pipeline_name_keeps_it(self, tmp_path: Path) -> None:
        root = project(
            tmp_path, "p1", entry("a", pipeline_name="shared"), entry("b", pipeline_name="shared")
        )
        assert run_sync(root, "native", "a", 5, native={"pipeline_name": "shared"})["status"] == (
            "success"
        )
        drop_schema(root, "raw_a")
        remove_source(root, "a", force=True)
        assert "shared" in pipeline_dirs()

    def test_the_override_name_is_the_dir_that_goes(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p1", entry("a", pipeline_name="custom"))
        run_sync(root, "native", "a", 5, native={"pipeline_name": "custom"})
        drop_schema(root, "raw_a")
        remove_source(root, "a", force=True)
        assert "custom" not in pipeline_dirs()

    def test_a_file_source_never_touches_a_same_named_pipeline(self, tmp_path: Path) -> None:
        other = project(tmp_path, "other", entry(NAME))
        run_sync(other, "native", NAME, 5)
        root = project(
            tmp_path,
            "p1",
            {
                "name": NAME,
                "type": "local_files",
                "local_files": {"directory": "data/uploads/ev", "file_pattern": "*.csv"},
            },
        )
        remove_source(root, NAME, force=True)
        assert NAME in pipeline_dirs()

    def test_a_dir_that_cannot_be_removed_only_warns(self, tmp_path: Path) -> None:
        root = synced(tmp_path, "native")
        drop_schema(root, f"raw_{NAME}")
        with patch("shutil.rmtree", side_effect=OSError("busy")):
            result = remove_source(root, NAME, force=True)
        assert result.status == "removed"
        assert NAME in pipeline_dirs()

    def test_the_server_pipelines_dir_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With DLT_DATA_DIR set (the cloud unit) only that dir is touched."""
        root = project(tmp_path, "p1", entry(NAME))
        local = tmp_path / "home" / ".dlt" / "pipelines" / NAME
        local.mkdir(parents=True)
        server = tmp_path / "server_data"
        monkeypatch.setenv("DLT_DATA_DIR", str(server))
        assert run_sync(root, "native", NAME, 5)["status"] == "success"
        drop_schema(root, f"raw_{NAME}")

        remove_source(root, NAME, force=True)
        assert not (server / "pipelines" / NAME).exists()
        assert local.exists()


def test_the_cli_says_once_that_the_local_state_was_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from dango.cli.main import cli

    root = synced(tmp_path, "native")
    drop_schema(root, f"raw_{NAME}")
    monkeypatch.chdir(root)
    monkeypatch.setenv("COLUMNS", "300")
    with patch(
        "dango.transformation.docs_locked.generate_dbt_docs_locked", return_value=(True, "")
    ):
        result: Any = CliRunner().invoke(cli, ["source", "remove", NAME, "--yes"])
    assert result.exit_code == 0, result.output
    assert result.output.count("Cleared the local sync state") == 1
    assert NAME not in pipeline_dirs()
