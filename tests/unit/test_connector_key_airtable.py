"""tests/unit/test_connector_key_airtable.py

Primary-key contract for the vendored Airtable connector: synthetic Airtable metadata/records through real dlt
into DuckDB for awkward primary-field names and record shapes.
"""

from __future__ import annotations

import pytest

from tests.factories.airtable_fake import airtable_record, airtable_source, airtable_table
from tests.factories.connector_contract import (
    KeyCase,
    fetch_rows,
    loaded_tables,
    make_pipeline,
    run_key_contract,
)

PRIMARY_NAMES = [
    "Name",
    "Team Member",
    "First Name",
    "name",
    "NAME",
    "Straße (Kürzel)",
    "Équipe",
    "2nd Name",
    "a.b",
    "dash-name",
    "Name (legacy)",
    "id",
    "_dlt_id",
    "Created Time",
    "createdTime",
    "fields",
]


def _two_people(primary: str, role_for_first: str = "dev") -> tuple[list, dict]:
    tables = [airtable_table("Team Members", primary)]
    records = {
        "tblAAAAAAAAAAAAAA": [
            airtable_record("rec1", **{primary: "Ann", "Role": role_for_first}),
            airtable_record("rec2", **{primary: "Bob", "Role": "ops"}),
        ]
    }
    return tables, records


def _case(primary: str) -> KeyCase:
    def open_(rev: int):
        tables, records = _two_people(primary, "lead" if rev else "dev")
        return airtable_source(tables, records)

    return KeyCase(
        id=f"airtable[{primary}]",
        open=open_,
        pk={"team_members": ("id",)},
        probe=("team_members", "id", "fields__role", "lead"),
        min_rows={"team_members": 2},
    )


@pytest.mark.unit
class TestAirtablePrimaryKeyContract:
    @pytest.mark.parametrize("primary", PRIMARY_NAMES)
    def test_any_primary_field_name_loads_with_a_real_key(self, primary, tmp_path):
        run_key_contract(_case(primary), tmp_path)

    def test_attachments_lookups_and_nested_values(self, tmp_path):
        tables = [airtable_table("Projects", "Project Title", ("Files", "Owners", "Meta"))]
        records = {
            "tblAAAAAAAAAAAAAA": [
                airtable_record(
                    "recP1",
                    **{
                        "Project Title": "Alpha",
                        "Files": [
                            {"id": "att1", "url": "https://x.invalid/a", "filename": "a.png"}
                        ],
                        "Owners": ["recU1", "recU2"],
                        "Meta": {"state": "open", "tags": ["x", "y"]},
                    },
                ),
                airtable_record("recP2", **{"Project Title": "Beta"}),
            ]
        }
        case = KeyCase(
            id="airtable[nested]",
            open=lambda rev: airtable_source(tables, records),
            pk={"projects": ("id",)},
            min_rows={"projects": 2},
        )
        run_key_contract(case, tmp_path)

    def test_records_with_a_blank_primary_field_load(self, tmp_path):
        # Airtable omits empty fields, so a blank primary value means no `fields.<name>` key at all.
        tables = [airtable_table("Team Members", "Name")]
        records = {
            "tblAAAAAAAAAAAAAA": [
                airtable_record("rec1", Name="Ann", Role="dev"),
                airtable_record("rec2", Role="ops"),
            ]
        }
        case = KeyCase(
            id="airtable[blank-primary]",
            open=lambda rev: airtable_source(tables, records),
            pk={"team_members": ("id",)},
            min_rows={"team_members": 2},
        )
        run_key_contract(case, tmp_path)

    def test_table_where_every_primary_value_is_blank_loads(self, tmp_path):
        tables = [airtable_table("Team Members", "Name")]
        records = {
            "tblAAAAAAAAAAAAAA": [
                airtable_record("rec1", Role="dev"),
                airtable_record("rec2", Role="ops"),
            ]
        }
        case = KeyCase(
            id="airtable[all-blank]",
            open=lambda rev: airtable_source(tables, records),
            pk={"team_members": ("id",)},
            min_rows={"team_members": 2},
        )
        run_key_contract(case, tmp_path)

    def test_empty_table_does_not_break_other_tables(self, tmp_path):
        tables = [
            airtable_table("Team Members", "Name", table_id="tblAAAAAAAAAAAAAA"),
            airtable_table("Empty One", "Title", table_id="tblEEEEEEEEEEEEEE"),
        ]
        records = {"tblAAAAAAAAAAAAAA": [airtable_record("rec1", Name="Ann", Role="dev")]}
        case = KeyCase(
            id="airtable[empty-table]",
            open=lambda rev: airtable_source(tables, records),
            pk={"team_members": ("id",)},
        )
        run_key_contract(case, tmp_path)

    def test_two_tables_with_different_primary_fields_in_one_run(self, tmp_path):
        tables = [
            airtable_table("Team Members", "Name", table_id="tblAAAAAAAAAAAAAA"),
            airtable_table("Open Tasks", "Task Title", ("Done",), table_id="tblBBBBBBBBBBBBBB"),
        ]
        records = {
            "tblAAAAAAAAAAAAAA": [airtable_record("rec1", Name="Ann", Role="dev")],
            "tblBBBBBBBBBBBBBB": [airtable_record("recT1", **{"Task Title": "Ship", "Done": "no"})],
        }
        case = KeyCase(
            id="airtable[two-tables]",
            open=lambda rev: airtable_source(tables, records),
            pk={"team_members": ("id",), "open_tasks": ("id",)},
        )
        run_key_contract(case, tmp_path)

    def test_two_tables_whose_names_normalise_to_the_same_table(self, tmp_path):
        # Airtable allows "Team Members" and "team-members" side by side; dlt snake_cases both to one table.
        tables = [
            airtable_table("Team Members", "Name", table_id="tblAAAAAAAAAAAAAA"),
            airtable_table("team-members", "Name", table_id="tblBBBBBBBBBBBBBB"),
        ]
        records = {
            "tblAAAAAAAAAAAAAA": [airtable_record("rec1", Name="Ann", Role="dev")],
            "tblBBBBBBBBBBBBBB": [airtable_record("rec2", Name="Bob", Role="ops")],
        }
        pipeline = make_pipeline(tmp_path)
        with airtable_source(tables, records) as source:
            pipeline.run(source)
        state = loaded_tables(pipeline)["team_members"]
        assert state.pk == ("id",)
        # replace disposition: the later resource owns the table; the load must not fail
        assert state.rows >= 1


@pytest.mark.unit
class TestAirtableDestinationColumnsUnchanged:
    """Existing destination column names must not change (user dbt models read them)."""

    @pytest.mark.parametrize("primary", ["Name", "Team Member"])
    def test_columns_are_record_id_created_time_and_flattened_fields(self, primary, tmp_path):
        pipeline = make_pipeline(tmp_path)
        tables, records = _two_people(primary)
        with airtable_source(tables, records) as source:
            pipeline.run(source)
        columns = set(loaded_tables(pipeline)["team_members"].columns)
        norm = pipeline.default_schema.naming.normalize_identifier
        expected = {"id", "created_time", f"fields__{norm(primary)}", "fields__role"}
        assert expected <= columns
        assert not {c for c in columns if c.startswith("fields__")} - {
            f"fields__{norm(primary)}",
            "fields__role",
        }
        assert fetch_rows(pipeline, "team_members", ("id",)) == [("rec1",), ("rec2",)]

    @pytest.mark.parametrize("primary", ["ID", "Created Time"])
    def test_primary_field_that_normalises_to_a_top_level_column_keeps_its_columns(
        self, primary, tmp_path
    ):
        # Guard: these names worked before the key change (the declared key matched a top-level column).
        pipeline = make_pipeline(tmp_path)
        tables, records = _two_people(primary)
        with airtable_source(tables, records) as source:
            pipeline.run(source)
        columns = set(loaded_tables(pipeline)["team_members"].columns)
        norm = pipeline.default_schema.naming.normalize_identifier
        assert {"id", "created_time", f"fields__{norm(primary)}", "fields__role"} <= columns
