"""tests/unit/test_empty_guard_airtable_narrowing.py

Narrowing an Airtable source's `table_names` (by name or by table id) after every table was synced:
the sync loads the chosen table and leaves the other tables of the base untouched. The real connector
runs against the fake `pyairtable` client, through `DltPipelineRunner.run_source` (real dlt + DuckDB).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from dango.config.models import DataSource, SourceType
from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.airtable_fake import airtable_record, airtable_stub, airtable_table

SCHEMA = "raw_airtable_test"
NAMES = {
    "tblAAAAAAAAAAAAAA": "Imported Table",
    "tblBBBBBBBBBBBBBB": "Team Members",
    "tblCCCCCCCCCCCCCC": "Projects",
    "tblDDDDDDDDDDDDDD": "Tasks",
}
LOADED = {"imported_table", "team_members", "projects", "tasks"}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_fake")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    with (
        patch("dango.ingestion.dlt_runner.time.sleep"),
        patch("dango.ingestion.dlt_runner._apply_dlt_telemetry_env"),
    ):
        yield root


def base(count=3, empty=()):
    """Four tables, each holding `count` records unless its id is in `empty`."""
    tables = [airtable_table(name, "Name", table_id=table_id) for table_id, name in NAMES.items()]
    records = {
        table_id: []
        if table_id in empty
        else [airtable_record(f"rec{table_id[3]}{i}", Name=f"{name} {i}") for i in range(count)]
        for table_id, name in NAMES.items()
    }
    return tables, records


def source(table_names=None) -> DataSource:
    config = {"base_id": "appX", "access_token_env": "AIRTABLE_ACCESS_TOKEN"}
    if table_names is not None:
        config["table_names"] = table_names
    return DataSource(name="airtable_test", type=SourceType.AIRTABLE, generic_config=config)


def snapshot(root: Path) -> dict[str, list[tuple]]:
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        out = {}
        for (name,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ? "
            "AND table_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\'",
            [SCHEMA],
        ).fetchall():
            out[name] = sorted(con.execute(f'SELECT * FROM "{SCHEMA}"."{name}"').fetchall())
        return out
    finally:
        con.close()


def sync(root, fake, table_names=None, **kwargs):
    with airtable_stub(*fake):
        return DltPipelineRunner(root).run_source(source(table_names), **kwargs)


def seed(root):
    assert sync(root, base())["status"] == "success"
    before = snapshot(root)
    assert set(before) == LOADED
    return before


@pytest.mark.unit
class TestAirtableTableNamesNarrowing:
    @pytest.mark.parametrize("selector", ["Imported Table", "tblAAAAAAAAAAAAAA"])
    def test_one_table_by_name_or_id_leaves_the_others_untouched(self, project, selector):
        before = seed(project)
        result = sync(project, base(count=1), [selector])
        assert result["status"] == "success", result.get("error")
        after = snapshot(project)
        assert len(after["imported_table"]) == 1  # the chosen table was rewritten
        for table in LOADED - {"imported_table"}:
            assert after[table] == before[table]

    def test_two_tables_leave_the_others_untouched(self, project):
        before = seed(project)
        result = sync(project, base(count=1), ["Projects", "Tasks"])
        assert result["status"] == "success", result.get("error")
        after = snapshot(project)
        assert after["team_members"] == before["team_members"]
        assert after["imported_table"] == before["imported_table"]
        assert len(after["projects"]) == 1 and len(after["tasks"]) == 1

    def test_the_chosen_table_losing_all_records_is_still_blocked(self, project):
        before = seed(project)
        result = sync(project, base(empty=("tblAAAAAAAAAAAAAA",)), ["Imported Table"])
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "imported_table: 3 rows" in result["error"]
        for table in LOADED - {"imported_table"}:
            assert table + ":" not in result["error"]
        assert snapshot(project) == before

    def test_allow_flag_empties_only_the_chosen_table(self, project):
        before = seed(project)
        result = sync(
            project,
            base(empty=("tblAAAAAAAAAAAAAA",)),
            ["Imported Table"],
            allow_empty_replace=True,
        )
        assert result["status"] == "success", result.get("error")
        after = snapshot(project)
        assert after["imported_table"] == []
        for table in LOADED - {"imported_table"}:
            assert after[table] == before[table]

    def test_syncing_every_table_again_after_narrowing_still_works(self, project):
        seed(project)
        assert sync(project, base(count=1), ["Tasks"])["status"] == "success"
        result = sync(project, base(count=2))
        assert result["status"] == "success", result.get("error")
        assert {t: len(r) for t, r in snapshot(project).items()} == dict.fromkeys(LOADED, 2)
