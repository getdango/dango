"""tests/unit/test_empty_guard_untouched_helpers.py

`replace_protected_tables` for tables this run cannot write: a destination table that no selected
resource can write is untouched; a table is protected when a selected resource owns it (fixed name,
schema owner or parent) or, with a computed-name selected resource, when ownership cannot be shown.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dango.ingestion.empty_guard import replace_protected_tables


def res(name, disposition="replace", table=None, selected=True):
    """A resource; `table=callable` is a computed table name."""
    return SimpleNamespace(
        name=name,
        table_name=name if table is None else table,
        write_disposition=disposition,
        selected=selected,
    )


def source(*resources):
    return SimpleNamespace(resources={r.name: r for r in resources})


def schema(**tables):
    return SimpleNamespace(default_schema=SimpleNamespace(tables=tables))


def root(owner, disposition="replace"):
    return {"resource": owner, "write_disposition": disposition}


def protected(src, pipeline, *candidates):
    return replace_protected_tables(src, pipeline, candidates)


@pytest.mark.unit
class TestTablesNoSelectedResourceCanWrite:
    def test_a_table_no_resource_names_is_untouched(self):
        assert protected(source(res("a")), schema(), "a", "orphan") == {"a"}

    def test_a_table_of_a_removed_resource_is_untouched(self):
        pipeline = schema(a=root("a"), gone=root("gone"))
        assert protected(source(res("a")), pipeline, "a", "gone") == {"a"}

    def test_a_table_of_an_unselected_resource_is_untouched(self):
        src = source(res("a"), res("b", selected=False))
        assert protected(src, schema(a=root("a"), b=root("b")), "a", "b") == {"a"}

    def test_a_table_without_a_schema_entry_is_untouched(self):
        assert protected(source(res("a")), schema(a=root("a")), "a", "not_in_schema") == {"a"}

    def test_no_selected_resource_protects_nothing(self):
        src = source(res("a", selected=False), res("b", selected=False))
        assert protected(src, schema(a=root("a"), b=root("b")), "a", "b") == set()

    def test_a_merge_selected_resource_does_not_protect_its_table(self):
        src = source(res("a", "merge"), res("b"))
        assert protected(src, schema(a=root("a", "merge"), b=root("b")), "a", "b") == {"b"}


@pytest.mark.unit
class TestOwnedTables:
    def test_a_stale_table_of_a_selected_resource_is_protected(self):
        """dlt empties every root table a selected replace resource wrote, even under an old name."""
        src = source(res("a", table="new"))
        pipeline = schema(old=root("a"), new=root("a"))
        assert protected(src, pipeline, "old", "new") == {"old", "new"}

    def test_a_dispatched_table_is_owned_by_its_resource(self):
        src = source(res("disp"))
        pipeline = schema(disp_a=root("disp"), disp_b=root("disp"), other=root("other"))
        assert protected(src, pipeline, "disp_a", "disp_b", "other") == {"disp_a", "disp_b"}

    def test_children_follow_their_parent(self):
        src = source(res("p"), res("q", selected=False), res("m", "merge"))
        pipeline = schema(
            p=root("p"),
            p__items={"parent": "p"},
            p__items__deep={"parent": "p__items"},
            q=root("q"),
            q__items={"parent": "q"},
            m=root("m", "merge"),
            m__items={"parent": "m"},
            gone=root("gone"),
            gone__items={"parent": "gone"},
        )
        tables = ["p", "p__items", "p__items__deep", "q", "q__items", "m", "m__items"]
        assert protected(src, pipeline, *tables, "gone", "gone__items") == {
            "p",
            "p__items",
            "p__items__deep",
        }

    def test_two_selected_resources_sharing_a_table_the_replace_one_wins(self):
        src = source(res("a", "merge", table="t"), res("b", "replace", table="t"))
        assert protected(src, schema(t=root("b")), "t") == {"t"}

    def test_a_fixed_name_hint_beats_a_stale_schema_owner(self):
        src = source(res("a", "merge", table="t"))
        assert protected(src, schema(t=root("a", "replace")), "t") == set()

    def test_an_unselected_hint_does_not_hide_a_selected_schema_owner(self):
        src = source(res("a", table=lambda item: "t"), res("u", "merge", table="t", selected=False))
        assert protected(src, schema(t=root("a")), "t") == {"t"}

    def test_a_selected_owner_whose_disposition_cannot_be_read_is_protected(self):
        src = source(res("a", disposition=lambda item: "merge"))
        assert protected(src, schema(t=root("a", "merge")), "t") == {"t"}

    def test_parent_cycle_does_not_loop(self):
        pipeline = schema(a={"parent": "b"}, b={"parent": "a"})
        assert protected(source(res("x")), pipeline, "a") == set()


@pytest.mark.unit
class TestOwnershipCannotBeShown:
    """A computed-name selected resource, an unreadable source or schema: prefer safe."""

    def computed(self):
        return source(res("dyn", table=lambda item: "x"), res("fx"))

    def test_with_a_computed_name_an_unowned_replace_table_stays_protected(self):
        pipeline = schema(fx=root("fx"), gone=root("gone"), gone_m=root("gone_m", "merge"))
        got = protected(self.computed(), pipeline, "fx", "gone", "gone_m", "not_in_schema")
        assert got == {"fx", "gone", "not_in_schema"}

    def test_with_a_computed_name_an_unselected_hint_stays_untouched(self):
        src = source(res("dyn", table=lambda item: "x"), res("u", table="u", selected=False))
        assert protected(src, schema(u=root("u")), "u") == set()

    def test_with_a_computed_name_a_selected_fixed_merge_table_is_still_not_protected(self):
        src = source(res("dyn", table=lambda item: "x"), res("m", "merge"))
        assert protected(src, schema(m=root("m", "merge")), "m") == set()

    def test_an_unreadable_source_protects_unknown_tables(self):
        assert protected(object(), schema(), "x") == {"x"}
        assert protected(None, schema(a=root("a", "merge")), "x", "a") == {"x"}

    def test_an_unreadable_schema_protects_tables_no_resource_names(self):
        broken = SimpleNamespace(default_schema=None)
        assert protected(source(res("a", "merge")), broken, "a", "x") == {"x"}
