"""tests/unit/test_dlt_state_cleaned_with_db_clean.py

`dango db clean` drops the local dlt pipeline state of the sources whose raw tables it removes, and only
the exact pipeline directories those tables name. Real dlt pipelines against a local DuckDB under a
throwaway HOME.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from dango.ingestion.sources.setup_lifecycle import remove_source
from tests.factories.dlt_state_projects import (
    db_clean,
    entry,
    ids,
    isolate_home,
    pipeline_dirs,
    pipelines_dir,
    project,
    run_sync,
    write_sources,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def orphan(root: Path, name: str) -> None:
    """Take `name` out of sources.yml (what `source remove` does to the config)."""
    write_sources(root, *[e for e in _entries(root) if e["name"] != name])


def _entries(root: Path) -> list[dict]:
    import yaml

    text = (root / ".dango" / "sources.yml").read_text(encoding="utf-8")
    return list(yaml.safe_load(text)["sources"])


def test_orphaned_sources_lose_their_pipeline_dir_and_configured_ones_keep_theirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("gone"), entry("kept"))
    run_sync(root, "native", "gone", 5)
    run_sync(root, "native", "kept", 5)
    orphan(root, "gone")

    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert pipeline_dirs() == ["kept"]
    assert result.output.count("Cleared the local sync state") == 1

    assert run_sync(root, "native", "kept", 8)["status"] == "success"
    assert ids(root, "kept") == list(range(1, 9))


def test_the_pipeline_name_override_is_found_in_the_warehouse_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("a", pipeline_name="custom"))
    run_sync(root, "native", "a", 5, native={"pipeline_name": "custom"})
    orphan(root, "a")
    db_clean(root, monkeypatch)
    assert pipeline_dirs() == []


def test_a_pipeline_name_a_configured_source_still_uses_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(
        tmp_path, "p1", entry("a", pipeline_name="shared"), entry("b", pipeline_name="shared")
    )
    for name in ("a", "b"):
        run_sync(root, "native", name, 5, native={"pipeline_name": "shared"})
    orphan(root, "a")
    db_clean(root, monkeypatch)
    assert pipeline_dirs() == ["shared"]
    assert run_sync(root, "native", "b", 8, native={"pipeline_name": "shared"})["status"] == (
        "success"
    )
    assert ids(root, "b") == list(range(1, 9))


def test_nothing_is_dropped_when_no_raw_table_is_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("kept"))
    run_sync(root, "native", "kept", 5)
    assert db_clean(root, monkeypatch).exit_code == 0
    assert pipeline_dirs() == ["kept"]


def test_a_removed_source_whose_pipeline_dir_is_already_gone_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("gone"))
    run_sync(root, "native", "gone", 5)
    remove_source(root, "gone", force=True)
    import shutil

    shutil.rmtree(pipelines_dir() / "gone")
    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Cleared the local sync state" not in result.output


def test_a_name_that_leaves_the_pipelines_dir_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The names come from the warehouse state table: a hostile one must not reach another folder."""
    root = project(tmp_path, "p1", entry("kept"))
    run_sync(root, "native", "kept", 5)
    outside = pipelines_dir().parent / "outside"
    outside.mkdir(parents=True)
    (outside / "state.json").write_text("{}", encoding="utf-8")
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    try:
        con.execute('CREATE SCHEMA "raw_evil"')
        con.execute('CREATE TABLE "raw_evil"."t" AS SELECT 1 AS id')
        con.execute(
            'CREATE TABLE "raw_evil"."_dlt_pipeline_state" AS SELECT \'../outside\' AS pipeline_name'
        )
    finally:
        con.close()

    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert (outside / "state.json").exists()


def test_a_failed_removal_of_the_dir_does_not_fail_the_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("gone"))
    run_sync(root, "native", "gone", 5)
    orphan(root, "gone")
    with patch("shutil.rmtree", side_effect=OSError("busy")):
        result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert pipeline_dirs() == ["gone"]


def test_the_server_pipelines_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project(tmp_path, "p1", entry("gone"))
    local = tmp_path / "home" / ".dlt" / "pipelines" / "gone"
    local.mkdir(parents=True)
    server = tmp_path / "server_data"
    monkeypatch.setenv("DLT_DATA_DIR", str(server))
    run_sync(root, "native", "gone", 5)
    orphan(root, "gone")
    db_clean(root, monkeypatch)
    assert not (server / "pipelines" / "gone").exists()
    assert local.exists()
