"""tests/unit/test_db_clean_dataset_names.py

`dango db clean` and the MCP orphan report never treat the tables of a configured source as orphaned,
whatever dataset the source loads into (custom `dataset_name`, names dlt rewrites, disabled sources).
Real dlt pipelines against a local DuckDB under a throwaway HOME, the real CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands import mcp_debug
from dango.ingestion.sources.setup_lifecycle import remove_source
from tests.factories.custom_dataset_projects import (
    ids_in,
    orphan_schemas,
    orphans,
    sources_of,
    synced_into,
    tables_by_schema,
)
from tests.factories.dlt_state_projects import (
    db_clean,
    entry,
    isolate_home,
    pipeline_dirs,
    project,
    write_sources,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def test_a_custom_dataset_survives_clean_and_the_next_sync_has_no_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    synced_into(root, "b", 5, "raw_custom")
    before = tables_by_schema(root)
    assert "raw_custom" in before
    assert orphans(root) == []

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "No orphaned tables found" in result.output
    assert "Dropped" not in result.output
    assert tables_by_schema(root) == before
    synced_into(root, "b", 8, "raw_custom")
    assert ids_in(root, "raw_custom") == upto(8)


@pytest.mark.parametrize(
    ("dataset", "schema"),
    [
        ("Raw_Custom", "raw_custom"),
        ("raw-dash", "raw_dash"),
        ("raw custom", "raw_custom"),
        ("Raw", "raw"),
        ("RAW_Mixed_CASE", "raw_mixed_case"),
        ("rawdata", "rawdata"),
        ("raw1", "raw1"),
    ],
)
def test_a_dataset_name_that_dlt_rewrites_is_still_the_sources_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str, schema: str
) -> None:
    root = project(tmp_path, "p1", *sources_of(b=dataset))
    synced_into(root, "b", 5, dataset)
    assert schema in tables_by_schema(root)

    assert orphans(root) == []
    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Kept" not in result.output
    assert ids_in(root, schema) == upto(5)


def test_a_source_name_that_dlt_rewrites_keeps_its_default_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`a_` loads into `raw_ax` (dlt replaces a trailing underscore with `x`)."""
    root = project(tmp_path, "p1", entry("a_"))
    synced_into(root, "a_", 5, None)
    assert "raw_ax" in tables_by_schema(root)

    assert orphans(root) == []
    assert db_clean(root, monkeypatch).exit_code == 0
    assert ids_in(root, "raw_ax") == upto(5)


def test_a_default_and_a_custom_source_are_both_kept_and_only_the_removed_one_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(a=None, b="raw_custom"))
    synced_into(root, "a", 5, None)
    synced_into(root, "b", 4, "raw_custom")
    assert db_clean(root, monkeypatch).exit_code == 0
    assert ids_in(root, "raw_a") == upto(5)
    assert ids_in(root, "raw_custom") == upto(4)

    write_sources(root, *sources_of(a=None))
    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Dropped raw_custom.events" in result.output
    assert "raw_custom" not in tables_by_schema(root)
    assert ids_in(root, "raw_a") == upto(5)
    assert pipeline_dirs() == ["a"]


def test_removing_a_custom_dataset_source_then_cleaning_drops_its_tables_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(a=None, b="raw_custom"))
    synced_into(root, "a", 5, None)
    synced_into(root, "b", 4, "raw_custom")
    remove_source(root, "b", force=True)
    assert "raw_custom" in orphan_schemas(root)

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "raw_custom" not in tables_by_schema(root)
    assert pipeline_dirs() == ["a"]


def test_a_disabled_source_is_still_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    synced_into(root, "b", 5, "raw_custom")
    write_sources(root, entry("b", dataset_name="raw_custom") | {"enabled": False})

    assert orphans(root) == []
    assert db_clean(root, monkeypatch).exit_code == 0
    assert ids_in(root, "raw_custom") == upto(5)


def test_default_named_sources_behave_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: default-named sources behave as before."""
    root = project(tmp_path, "p1", entry("kept"), entry("gone"))
    synced_into(root, "kept", 5, None)
    synced_into(root, "gone", 5, None)
    write_sources(root, entry("kept"))

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert tables_by_schema(root).keys() >= {"raw_kept"}
    assert "raw_gone" not in tables_by_schema(root)
    assert pipeline_dirs() == ["kept"]


def test_the_mcp_orphan_report_uses_the_same_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(a=None, b="raw_custom", c="Raw-Dash"))
    synced_into(root, "a", 3, None)
    synced_into(root, "b", 3, "raw_custom")
    synced_into(root, "c", 3, "Raw-Dash")
    write_sources(root, *sources_of(a=None, b="raw_custom", c="Raw-Dash"))
    monkeypatch.setattr(mcp_debug, "_get_project_root", lambda: root)

    report = mcp_debug.get_warehouse_health()
    assert report["orphaned_tables"] == []

    write_sources(root, *sources_of(a=None))
    report = mcp_debug.get_warehouse_health()
    listed = {item["schema"] for item in report["orphaned_tables"]}
    assert listed == {"raw_custom", "raw_dash"}


def test_duplicate_source_names_still_stop_the_clean_before_anything_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("a"))
    synced_into(root, "a", 5, None)
    before = tables_by_schema(root)
    write_sources(root, entry("a"), entry("A"))

    result = db_clean(root, monkeypatch)

    assert result.exit_code != 0
    assert "Duplicate source name" in result.output
    assert tables_by_schema(root) == before


def test_an_empty_warehouse_and_a_project_without_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1")
    assert db_clean(root, monkeypatch).exit_code == 0
    (root / "data").mkdir()
    import duckdb

    duckdb.connect(str(root / "data" / "warehouse.duckdb")).close()
    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "No orphaned tables found" in result.output
