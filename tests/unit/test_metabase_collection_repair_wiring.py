"""tests/unit/test_metabase_collection_repair_wiring.py

Save-side duplicate handling, repair report, pure planner helpers and start/serve wiring (K-34).
"""

import json
from unittest.mock import patch

import pytest
import yaml
from rich.console import Console

from dango.visualization.dashboard_manager import DashboardManager, import_dashboards
from tests.factories.metabase_collection_fakes import FakeMetabase

REPORT = ".dango/state/metabase_collection_repair.json"


@pytest.fixture
def fake():
    with FakeMetabase().installed() as f:
        yield f


@pytest.fixture
def manager(tmp_path):
    return DashboardManager(tmp_path, metabase_url="http://mb.test", session_token="tok")


def _write_collections(tmp_path, rows):
    mdir = tmp_path / "metabase"
    mdir.mkdir(exist_ok=True)
    data = [{"id": i, "name": n, "parent_id": p} for i, n, p in rows]
    (mdir / "collections.yml").write_text(yaml.safe_dump({"collections": data}))


class TestSaveToFiles:
    def test_empty_duplicates_collapse_to_one_source_row(self, manager, fake, tmp_path):
        for cid in (5, 7, 8):
            fake.add("Shared", cid=cid)
        manager.save_to_files()
        rows = yaml.safe_load((tmp_path / "metabase/collections.yml").read_text())["collections"]
        assert [r["name"] for r in rows] == ["Shared"]
        assert fake.mutations == []

    def test_populated_duplicates_fail_save_without_writing(self, manager, fake, tmp_path):
        fake.add("Shared", cid=5, items=1)
        fake.add("Shared", cid=7, items=2)
        summary = manager.save_to_files()
        assert summary["success"] is False
        assert "5" in " ".join(summary["errors"]) and "7" in " ".join(summary["errors"])
        assert not (tmp_path / "metabase/collections.yml").exists()
        assert fake.mutations == []

    def test_equal_names_under_different_parents_are_not_duplicates(self, manager, fake, tmp_path):
        a = fake.add("A")
        fake.add("Shared")
        fake.add("Shared", parent=a)
        summary = manager.save_to_files(collections=["Shared"])
        assert not summary["errors"] or "duplicate" not in " ".join(summary["errors"]).lower()
        rows = yaml.safe_load((tmp_path / "metabase/collections.yml").read_text())["collections"]
        assert len(rows) == 2


class TestRepairReport:
    def test_report_is_atomic_non_secret_and_reversible(self, manager, fake, tmp_path):
        for cid in (5, 7, 8):
            fake.add("Shared", cid=cid)
        _write_collections(tmp_path, [(5, "Shared", None)])
        manager.load_from_files()
        report = json.loads((tmp_path / REPORT).read_text())
        assert report["archived_ids"] == [7, 8]
        assert {"timestamp", "dango_version", "export_fingerprint", "kept_ids", "errors"} <= set(
            report
        )
        assert "tok" not in json.dumps(report)
        assert list((tmp_path / ".dango/state").glob("*.tmp*")) == []

    def test_second_start_is_a_noop_and_claims_no_repair(self, manager, fake, tmp_path):
        for cid in (5, 7, 8):
            fake.add("Shared", cid=cid)
        _write_collections(tmp_path, [(5, "Shared", None)])
        manager.load_from_files()
        before = (tmp_path / REPORT).read_text()
        fake.mutations.clear()
        second = manager.load_from_files()
        assert fake.mutations == []
        assert second["collection_repair"]["archived"] == 0
        assert (tmp_path / REPORT).read_text() == before


class TestPlannerHelpers:
    def test_normalize_export_orders_parents_first_and_merges_source_ids(self):
        from dango.visualization.collection_repair import normalize_export

        entries = normalize_export(
            [
                {"id": 3, "name": "Child", "parent_id": 2},
                {"id": 1, "name": "Shared", "parent_id": None},
                {"id": 2, "name": "Shared", "parent_id": None},
            ]
        )
        assert [e.path for e in entries] == [("Shared",), ("Shared", "Child")]
        assert entries[0].source_ids == (1, 2)

    def test_canonical_choice_is_deterministic_regardless_of_input_order(self):
        from dango.visualization.collection_repair import LiveCollection, select_canonical

        def cands(ids):
            return [LiveCollection(i, "Shared", None, ("Shared",)) for i in ids]

        counts = {4: 0, 9: 0, 12: 0}
        first = select_canonical(cands([12, 4, 9]), (), counts, {})
        second = select_canonical(cands([9, 12, 4]), (), counts, {})
        assert first == second
        assert first.canonical == 4 and first.archive == (9, 12)


class TestStartAndServeWiring:
    def test_startup_import_returns_repair_summary(self, fake, tmp_path):
        from dango.platform.common.startup import import_dashboards as startup_import

        for cid in (5, 7):
            fake.add("Shared", cid=cid)
        _write_collections(tmp_path, [(5, "Shared", None)])
        with (
            patch.object(
                DashboardManager,
                "_load_credentials",
                lambda self: setattr(self, "session_token", "tok"),
            ),
            patch(
                "dango.visualization.dashboard_manager.resolve_metabase_url",
                return_value="http://mb.test",
            ),
        ):
            result = startup_import(tmp_path)
        assert result is not None
        assert result["collection_repair"]["archived"] == 1
        assert fake.archived_ids() == [7]

    def test_legacy_wrapper_passes_repair_summary_through(self, fake, tmp_path):
        fake.add("Shared", cid=5)
        _write_collections(tmp_path, [(5, "Shared", None)])
        with (
            patch.object(
                DashboardManager,
                "_load_credentials",
                lambda self: setattr(self, "session_token", "tok"),
            ),
            patch(
                "dango.visualization.dashboard_manager.resolve_metabase_url",
                return_value="http://mb.test",
            ),
        ):
            result = import_dashboards(tmp_path)
        assert result["collection_repair"]["archived"] == 0

    def test_local_start_message_states_count_and_report_location(self):
        from dango.cli.commands.platform import print_collection_repair

        console = Console(record=True, width=200)
        print_collection_repair(
            console,
            {
                "collection_repair": {
                    "archived": 27,
                    "complete": True,
                    "errors": [],
                    "ambiguous_ids": [],
                    "report_path": ".dango/state/metabase_collection_repair.json",
                }
            },
        )
        text = console.export_text()
        assert "27" in text and "metabase_collection_repair.json" in text

    def test_local_start_message_is_silent_when_nothing_was_repaired(self):
        from dango.cli.commands.platform import print_collection_repair

        console = Console(record=True, width=200)
        print_collection_repair(
            console,
            {
                "collection_repair": {
                    "archived": 0,
                    "complete": True,
                    "errors": [],
                    "ambiguous_ids": [],
                }
            },
        )
        assert console.export_text().strip() == ""

    def test_shared_import_layer_logs_repair_outcome_for_serve(self, manager, fake, tmp_path):
        for cid in (5, 7):
            fake.add("Shared", cid=cid)
        _write_collections(tmp_path, [(5, "Shared", None)])
        with patch("dango.visualization.dashboard_manager.logger") as log:
            manager.load_from_files()
        assert log.info.called or log.warning.called

    def test_dry_run_message_lists_planned_actions(self):
        from dango.cli.commands.platform import print_collection_repair

        console = Console(record=True, width=200)
        print_collection_repair(
            console,
            {
                "collection_repair": {
                    "planned": ["archive empty duplicate collection 7 at 'Shared'"]
                }
            },
        )
        assert "Would archive empty duplicate collection 7" in console.export_text()
