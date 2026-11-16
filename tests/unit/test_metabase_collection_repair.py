"""tests/unit/test_metabase_collection_repair.py

Failure-mode matrix for idempotent collection import and empty-duplicate repair (K-34).
"""

import json

import pytest
import yaml

from dango.visualization.dashboard_manager import DashboardManager
from tests.factories.metabase_collection_fakes import FakeMetabase


@pytest.fixture
def fake():
    with FakeMetabase().installed() as f:
        yield f


@pytest.fixture
def manager(tmp_path):
    return DashboardManager(tmp_path, metabase_url="http://mb.test", session_token="tok")


def _export(*rows):
    return [{"id": r[0], "name": r[1], "parent_id": r[2]} for r in rows]


def _write_export(tmp_path, rows, dashboards=()):
    mdir = tmp_path / "metabase"
    mdir.mkdir(exist_ok=True)
    (mdir / "collections.yml").write_text(yaml.safe_dump({"collections": _export(*rows)}))
    if dashboards:
        (mdir / "dashboards").mkdir(exist_ok=True)
        for i, name in enumerate(dashboards):
            (mdir / "dashboards" / f"d{i}.yml").write_text(
                yaml.safe_dump({"name": name, "collection_id": rows[0][0], "cards": []})
            )


def _report(tmp_path):
    return json.loads((tmp_path / ".dango/state/metabase_collection_repair.json").read_text())


def test_first_import_into_empty_metabase_creates_each_unique_path_once(manager, fake):
    rows = [(5, "Shared", None), (6, "Sub", 5), (7, "Other", None)]
    mapping = manager._import_collections(_export(*rows))
    posts = [m for m in fake.mutations if m[0] == "POST"]
    assert len(posts) == 3
    new_shared, new_sub = mapping[5], mapping[6]
    sub = next(c for c in fake.active() if c["id"] == new_sub)
    assert sub["location"] == f"/{new_shared}/"
    assert len({mapping[5], mapping[6], mapping[7]}) == 3


def test_same_import_repeated_makes_zero_collection_mutations(manager, fake):
    rows = _export((5, "Shared", None), (6, "Sub", 5))
    first = manager._import_collections(rows)
    fake.mutations.clear()
    second = manager._import_collections(rows)
    assert fake.mutations == []
    assert second == first
    assert len(fake.active_named("Shared")) == 1


def test_equal_child_names_under_different_parents_stay_distinct(manager, fake):
    rows = _export((1, "A", None), (2, "B", None), (3, "Child", 1), (4, "Child", 2))
    mapping = manager._import_collections(rows)
    assert mapping[3] != mapping[4]
    fake.mutations.clear()
    assert manager._import_collections(rows) == mapping
    assert fake.mutations == []
    # Live tree already has A/Child only: B/Child must still be created.
    other = FakeMetabase()
    a = other.add("A")
    other.add("B")
    other.add("Child", a)
    with other.installed():
        partial = manager._import_collections(rows)
    posts = [m[2]["name"] for m in other.mutations if m[0] == "POST"]
    assert posts == ["Child"]
    assert partial[3] != partial[4]


def test_28_empty_root_shared_siblings_keep_canonical_and_archive_27(manager, fake):
    fake.add("Shared", cid=5)
    for cid in range(7, 34):
        fake.add("Shared", cid=cid)
    mapping = manager._import_collections(_export((5, "Shared", None)))
    assert mapping == {5: 5}
    assert [c["id"] for c in fake.active_named("Shared")] == [5]
    assert fake.archived_ids() == list(range(7, 34))
    assert not [m for m in fake.mutations if m[0] == "POST"]
    assert manager.last_collection_result.archived_ids == list(range(7, 34))


def test_populated_duplicate_is_canonical_and_only_empties_are_archived(manager, fake):
    fake.add("Shared", cid=5)
    fake.add("Shared", cid=7, items=3)
    fake.add("Shared", cid=8)
    mapping = manager._import_collections(_export((5, "Shared", None)))
    assert mapping == {5: 7}
    assert fake.archived_ids() == [5, 8]


def test_two_populated_duplicates_archive_neither_and_report_ambiguity(manager, fake, tmp_path):
    fake.add("Shared", cid=5, items=1)
    fake.add("Shared", cid=7, items=2)
    fake.add("Shared", cid=8)
    mapping = manager._import_collections(_export((5, "Shared", None)))
    assert mapping == {5: 5}
    assert fake.archived_ids() == [8]
    result = manager.last_collection_result
    assert result.ambiguous_ids == [7]
    assert result.complete is False
    assert _report(tmp_path)["ambiguous_ids"] == [7]


@pytest.mark.parametrize("mode", ["error", "malformed"])
def test_unknown_item_listing_archives_nothing(manager, fake, tmp_path, mode):
    fake.add("Shared", cid=5)
    fake.add("Shared", cid=7)
    fake.add("Shared", cid=8)
    if mode == "error":
        fake.fail("GET", "/api/collection/7/items", 500)
    else:
        fake.malformed_items.add(7)
    mapping = manager._import_collections(_export((5, "Shared", None)))
    assert mapping == {5: 5}
    assert fake.archived_ids() == []
    assert not [m for m in fake.mutations if m[0] == "POST"]
    assert manager.last_collection_result.errors
    assert _report(tmp_path)["errors"]


def test_archive_failure_keeps_progress_and_next_run_completes(manager, fake, tmp_path):
    for cid in (5, 7, 8, 9):
        fake.add("Shared", cid=cid)
    fake.fail("PUT", "/api/collection/9", 500)
    rows = _export((5, "Shared", None))
    manager._import_collections(rows)
    first = manager.last_collection_result
    assert fake.archived_ids() == [7, 8]
    assert first.complete is False and first.errors
    assert not [m for m in fake.mutations if m[0] == "POST"]
    manager._import_collections(rows)
    assert fake.archived_ids() == [7, 8, 9]
    assert manager.last_collection_result.complete is True
    assert not [m for m in fake.mutations if m[0] == "POST"]
    assert _report(tmp_path)["errors"] == []


def test_duplicate_rows_in_collections_yml_collapse_by_path(manager, fake):
    rows = _export((5, "Shared", None), (6, "Shared", None), (7, "Child", 6))
    mapping = manager._import_collections(rows)
    assert mapping[5] == mapping[6]
    assert [m[2]["name"] for m in fake.mutations if m[0] == "POST"] == ["Shared", "Child"]
    child = next(c for c in fake.active() if c["id"] == mapping[7])
    assert child["location"] == f"/{mapping[5]}/"


@pytest.mark.parametrize(
    "rows",
    [
        [(1, "Root", None), (2, "Orphan", 999)],
        [(1, "A", 2), (2, "B", 1)],
        [(1, "Self", 1)],
    ],
    ids=["broken", "cycle", "self"],
)
def test_broken_or_cyclic_parent_refs_fail_without_mutation(manager, fake, rows):
    with pytest.raises(ValueError, match="parent"):
        manager._import_collections(_export(*rows))
    assert fake.mutations == []


def test_later_import_failure_archives_only_collections_created_by_this_run(
    manager, fake, tmp_path
):
    existing = fake.add("Existing")
    _write_export(tmp_path, [(1, "Existing", None), (2, "Brand New", None)], dashboards=["Dash"])
    fake.dashboard_post_status = 500
    result = manager.load_from_files()
    assert result["success"] is False
    new_id = next(c["id"] for c in fake.collections if c["name"] == "Brand New")
    assert fake.archived_ids() == [new_id]
    assert existing in [c["id"] for c in fake.active()]
    assert not any(p.startswith("/api/collection") for p in fake.deletes)


def test_no_collections_file_keeps_startup_behavior_without_global_cleanup(manager, fake, tmp_path):
    for cid in (5, 7, 8):
        fake.add("Shared", cid=cid)
    mdir = tmp_path / "metabase" / "dashboards"
    mdir.mkdir(parents=True)
    (mdir / "d.yml").write_text(yaml.safe_dump({"name": "Dash", "cards": []}))
    manager.load_from_files()
    assert fake.archived_ids() == []
    assert not [m for m in fake.mutations if m[:2] == ("POST", "/api/collection")]
    assert manager.last_collection_result is None


def test_dry_run_reports_planned_actions_with_zero_mutations(manager, fake, tmp_path):
    for cid in (5, 7, 8):
        fake.add("Shared", cid=cid)
    _write_export(tmp_path, [(5, "Shared", None), (9, "Fresh", None)])
    result = manager.load_from_files(dry_run=True)
    repair = result["collection_repair"]
    assert repair["dry_run"] is True
    joined = " ".join(repair["planned"])
    assert "archive" in joined and "create" in joined
    assert fake.mutations == []
    assert not (tmp_path / ".dango/state/metabase_collection_repair.json").exists()


@pytest.mark.parametrize("status", [401, 503])
def test_transient_failure_deletes_nothing_and_next_start_repairs(manager, fake, tmp_path, status):
    for cid in (5, 7):
        fake.add("Shared", cid=cid)
    _write_export(tmp_path, [(5, "Shared", None)])
    fake.fail("GET", "/api/collection", status)
    first = manager.load_from_files()
    assert first["success"] is False
    assert any("retry" in e.lower() for e in first["errors"])
    assert fake.mutations == []
    second = manager.load_from_files()
    assert fake.archived_ids() == [7]
    assert second["collection_repair"]["archived"] == 1
