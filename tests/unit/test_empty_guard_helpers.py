"""tests/unit/test_empty_guard_helpers.py

Per-table scoping helpers of the empty-replace guard (dango/ingestion/empty_guard.py): which tables
are protected for real REST API / vendored / custom sources, and the failure message wording.
"""

from __future__ import annotations

from types import SimpleNamespace

import dlt
import pytest
from dlt.sources.rest_api import rest_api_source

from dango.ingestion.dlt_sources.chess import source as chess_source
from dango.ingestion.empty_guard import (
    replace_protected_tables,
    resource_dispositions,
    truncate_message,
    zero_rows_message,
)

NO_SCHEMA = SimpleNamespace(default_schema=SimpleNamespace(tables={}))


def _schema(tables):
    return SimpleNamespace(default_schema=SimpleNamespace(tables=tables))


@pytest.mark.unit
class TestResourceDispositions:
    def test_rest_api_source_reads_each_endpoint(self):
        source = rest_api_source(
            {
                "client": {"base_url": "https://example.invalid/"},
                "resources": [
                    {"name": "a", "write_disposition": "replace", "endpoint": {"path": "a"}},
                    {
                        "name": "b",
                        "write_disposition": "merge",
                        "primary_key": "id",
                        "endpoint": {"path": "b"},
                    },
                    {"name": "c", "endpoint": {"path": "c"}},
                ],
            }
        )
        assert resource_dispositions(source) == {"a": "replace", "b": "merge", "c": "append"}
        assert replace_protected_tables(source, NO_SCHEMA, ["a", "b", "c"]) == {"a"}

    def test_vendored_source_protects_only_its_replace_table(self):
        source = chess_source(players=["magnuscarlsen"])
        dispositions = resource_dispositions(source)
        assert dispositions["players_profiles"] == "replace"
        assert dispositions["players_games"] == "append"
        # `players_archives` is replace-mode but a deselected feed: its table is never written.
        assert dispositions["players_archives"] == "untouched"
        tables = ["players_profiles", "players_games", "players_archives", "players_online_status"]
        assert replace_protected_tables(source, NO_SCHEMA, tables) == {"players_profiles"}

    def test_disposition_given_as_a_mapping(self):
        @dlt.resource(name="h", write_disposition={"disposition": "merge", "strategy": "scd2"})
        def _h():
            yield {"id": 1}

        @dlt.source(name="s")
        def _s():
            yield _h

        assert resource_dispositions(_s()) == {"h": "merge"}

    def test_unrecognised_source_gives_no_hints(self):
        assert resource_dispositions(object()) == {}
        assert resource_dispositions(None) == {}


@pytest.mark.unit
class TestReplaceProtectedTables:
    def test_unknown_table_is_protected(self):
        source = SimpleNamespace(resources={})
        assert replace_protected_tables(source, NO_SCHEMA, ["orphan"]) == {"orphan"}

    def test_schema_answers_for_a_table_no_resource_names(self):
        tables = {
            "dyn_a": {"write_disposition": "merge"},
            "dyn_a__items": {"parent": "dyn_a"},
            "dyn_b": {"write_disposition": "replace"},
        }
        source = SimpleNamespace(resources={})
        got = replace_protected_tables(source, _schema(tables), ["dyn_a", "dyn_a__items", "dyn_b"])
        assert got == {"dyn_b"}

    def test_resource_hint_wins_over_a_stale_schema_entry(self):
        resource = SimpleNamespace(table_name="t", write_disposition="merge", selected=True)
        source = SimpleNamespace(resources={"t": resource})
        stale = _schema({"t": {"write_disposition": "replace"}})
        assert replace_protected_tables(source, stale, ["t"]) == set()

    def test_unreadable_schema_protects_everything(self):
        broken = SimpleNamespace(default_schema=None)
        source = SimpleNamespace(resources={})
        assert replace_protected_tables(source, broken, ["x"]) == {"x"}

    def test_parent_cycle_does_not_loop(self):
        tables = {"a": {"parent": "b"}, "b": {"parent": "a"}}
        got = replace_protected_tables(SimpleNamespace(resources={}), _schema(tables), ["a"])
        assert got == {"a"}


@pytest.mark.unit
class TestMessages:
    def test_truncate_message_names_the_tables_and_the_reason(self):
        text = truncate_message("src", [("dim", 2), ("other", 1000)])
        assert text.startswith("Sync would truncate 2 table(s) with existing data:")
        assert "dim: 2 rows → 0 rows (table would be lost)" in text
        assert "other: 1,000 rows → 0 rows" in text
        assert "Replace-mode tables are rewritten on every sync" in text
        assert text.endswith("dango sync src --allow-empty-replace")

    def test_zero_rows_message_keeps_its_prefix_and_names_the_tables(self):
        text = zero_rows_message("src", 5000, ["b", "a"])
        assert text.startswith("Sync returned 0 rows — existing 5,000 rows preserved")
        assert "replace-mode table(s): a, b" in text
        assert "dango sync src --allow-empty-replace" in text

    def test_zero_rows_message_without_table_names(self):
        assert "table(s)" not in zero_rows_message("src", 3, None)
