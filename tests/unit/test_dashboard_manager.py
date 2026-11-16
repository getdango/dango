"""tests/unit/test_dashboard_manager.py

Unit tests for dashboard_manager module: _parse_parent_id, collection import and rollback.
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.visualization.collection_repair import CollectionApiError
from dango.visualization.dashboard_manager import DashboardManager
from tests.factories.metabase_collection_fakes import FakeMetabase


@pytest.fixture
def dashboard_manager(tmp_path):
    """Create a DashboardManager instance with mocked credentials."""
    manager = DashboardManager(
        project_root=tmp_path,
        metabase_url="http://localhost:3000",
        session_token="test-token",
    )
    return manager


class TestParseParentId:
    """Tests for _parse_parent_id helper."""

    def test_parse_parent_id_root(self, dashboard_manager):
        """Root collections have location '/' → None."""
        assert dashboard_manager._parse_parent_id("/") is None

    def test_parse_parent_id_single_collection(self, dashboard_manager):
        """Child of collection 1 (Metabase location lists ancestors only) → parent 1."""
        assert dashboard_manager._parse_parent_id("/1/") == 1

    def test_parse_parent_id_nested(self, dashboard_manager):
        """Nested collection '/1/5/' → parent 5."""
        assert dashboard_manager._parse_parent_id("/1/5/") == 5

    def test_parse_parent_id_deep_nested(self, dashboard_manager):
        """Deep nested '/1/5/7/' → parent 7."""
        assert dashboard_manager._parse_parent_id("/1/5/7/") == 7

    def test_parse_parent_id_very_deep(self, dashboard_manager):
        """Very deep '/1/2/3/4/5/' → parent 5."""
        assert dashboard_manager._parse_parent_id("/1/2/3/4/5/") == 5

    def test_parse_parent_id_no_leading_slash(self, dashboard_manager):
        """Location without leading/trailing slashes."""
        assert dashboard_manager._parse_parent_id("1/5") == 5

    def test_parse_parent_id_empty_string(self, dashboard_manager):
        """Empty string → None."""
        assert dashboard_manager._parse_parent_id("") is None

    def test_parse_parent_id_null_location(self, dashboard_manager):
        """None/null location returns None instead of AttributeError."""
        # If Metabase returns location: null (not absent, but null value)
        assert dashboard_manager._parse_parent_id(None) is None


class TestImportCollections:
    """Tests for _import_collections method (validation and API-failure handling)."""

    def test_import_collections_missing_id(self, dashboard_manager):
        """Missing 'id' field raises ValueError."""
        collections = [{"name": "Test", "parent_id": None}]  # missing 'id'
        with pytest.raises(ValueError, match="missing or invalid 'id'"):
            dashboard_manager._import_collections(collections)

    def test_import_collections_missing_name(self, dashboard_manager):
        """Missing 'name' field raises ValueError."""
        collections = [{"id": 1, "parent_id": None}]  # missing 'name'
        with pytest.raises(ValueError, match="missing or invalid 'name'"):
            dashboard_manager._import_collections(collections)

    def test_import_collections_invalid_parent_id_type(self, dashboard_manager):
        """Invalid parent_id type (string) raises ValueError."""
        collections = [{"id": 1, "name": "Test", "parent_id": "5"}]  # string instead of int
        with pytest.raises(ValueError, match="invalid 'parent_id'"):
            dashboard_manager._import_collections(collections)

    def test_parse_parent_id_non_numeric(self, dashboard_manager):
        """Non-numeric parent ID returns None instead of crashing."""
        # If Metabase returns malformed location with non-numeric parent
        assert dashboard_manager._parse_parent_id("/admin/5/") is None
        assert dashboard_manager._parse_parent_id("/invalid/path/") is None

    def test_import_collections_empty_list(self, dashboard_manager):
        """Empty collections list returns empty mapping without calling Metabase."""
        with patch("dango.visualization.dashboard_manager.requests") as mock_requests:
            assert dashboard_manager._import_collections([]) == {}
        assert not mock_requests.method_calls

    def test_import_collections_topological_order(self, dashboard_manager):
        """Parents are created before children, children under the mapped parent."""
        fake = FakeMetabase()
        with fake.installed():
            mapping = dashboard_manager._import_collections(
                [
                    {"id": 3, "name": "Child", "parent_id": 2},
                    {"id": 2, "name": "Root2", "parent_id": None},
                    {"id": 1, "name": "Root1", "parent_id": None},
                ]
            )
        posts = [m[2] for m in fake.mutations if m[0] == "POST"]
        assert [p["name"] for p in posts].index("Child") == 2
        assert posts[2]["parent_id"] == mapping[2]
        assert set(mapping) == {1, 2, 3}

    def test_import_collections_create_failure_still_adds_mapping(self, dashboard_manager):
        """A failed create maps to the parent (or root 1) and records an error."""
        fake = FakeMetabase()
        fake.fail("POST", "/api/collection", 500, 500)
        with fake.installed():
            mapping = dashboard_manager._import_collections(
                [
                    {"id": 1, "name": "Root", "parent_id": None},
                    {"id": 2, "name": "Child", "parent_id": 1},
                ]
            )
        assert mapping == {1: 1, 2: 1}
        assert dashboard_manager.last_collection_result.errors
        assert fake.mutations == []

    def test_import_collections_missing_id_in_response_is_an_error_with_fallback(
        self, dashboard_manager
    ):
        """A 200 response without an id is treated as a failed create."""
        with patch("dango.visualization.collection_repair_api.requests") as mock_requests:
            mock_requests.get.return_value = MagicMock(status_code=200, json=lambda: [])
            mock_requests.post.return_value = MagicMock(status_code=200, json=lambda: {})
            mapping = dashboard_manager._import_collections(
                [{"id": 1, "name": "Test", "parent_id": None}]
            )
        assert mapping == {1: 1}
        assert dashboard_manager.last_collection_result.errors

    def test_import_collections_listing_failure_raises_without_mutation(self, dashboard_manager):
        fake = FakeMetabase()
        fake.fail("GET", "/api/collection", 500)
        with fake.installed(), pytest.raises(CollectionApiError, match="retry"):
            dashboard_manager._import_collections([{"id": 1, "name": "X", "parent_id": None}])
        assert fake.mutations == []


class TestCreateDashboardFromYamlCardAttach:
    """Tests for _create_dashboard_from_yaml's card-attach step.

    Metabase 0.62 removed POST /api/dashboard/:id/cards (404s now); cards
    are attached via PUT /api/dashboard/:id with a full `dashcards` array
    instead. See dango/visualization/dashboard_manager.py.
    """

    @patch("dango.visualization.dashboard_manager.requests.put")
    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_attaches_cards_via_put_with_dashcards_array(
        self, mock_post, mock_put, dashboard_manager
    ):
        """Successful card creation + attach uses PUT with a dashcards array."""
        # First POST creates the dashboard, second+ POSTs create cards
        dashboard_response = MagicMock(status_code=200)
        dashboard_response.json.return_value = {"id": 99}
        card_response_1 = MagicMock(status_code=200)
        card_response_1.json.return_value = {"id": 11}
        card_response_2 = MagicMock(status_code=200)
        card_response_2.json.return_value = {"id": 12}
        mock_post.side_effect = [dashboard_response, card_response_1, card_response_2]

        mock_put.return_value = MagicMock(status_code=200)

        with patch.object(dashboard_manager, "get_dashboards", return_value=[]):
            dashboard_data = {
                "name": "Test Dashboard",
                "description": "desc",
                "cards": [
                    {
                        "name": "Card A",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}},
                        "display": "table",
                        "position": {"row": 0, "col": 0, "size_x": 6, "size_y": 4},
                    },
                    {
                        "name": "Card B",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 2"}},
                        "display": "table",
                        "position": {"row": 0, "col": 6, "size_x": 6, "size_y": 4},
                    },
                ],
            }

            result = dashboard_manager._create_dashboard_from_yaml(dashboard_data)

        assert result["dashboard_id"] == 99
        assert set(result["card_ids"]) == {11, 12}
        mock_put.assert_called_once()
        put_call = mock_put.call_args
        assert put_call[0][0] == "http://localhost:3000/api/dashboard/99"
        dashcards = put_call[1]["json"]["dashcards"]
        assert len(dashcards) == 2
        assert {c["card_id"] for c in dashcards} == {11, 12}
        # Each new dashcard needs a unique negative placeholder id
        assert {c["id"] for c in dashcards} == {-1, -2}
        assert dashcards[0]["row"] == 0
        assert dashcards[0]["col"] == 0
        assert dashcards[0]["size_x"] == 6
        assert dashcards[0]["size_y"] == 4
        # Old removed endpoint must never be hit
        for call in mock_post.call_args_list:
            assert not call[0][0].endswith("/cards")

    @patch("dango.visualization.dashboard_manager.requests.put")
    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_create_dashboard_from_yaml_returns_card_ids(
        self, mock_post, mock_put, dashboard_manager
    ):
        """Direct test of the changed return shape: _create_dashboard_from_yaml
        must surface the embedded cards' IDs alongside the dashboard's own ID,
        not just the dashboard ID, so load_from_files() can track them for
        rollback (1.0.8-Q10 fix #2)."""
        dashboard_response = MagicMock(status_code=200)
        dashboard_response.json.return_value = {"id": 77}
        card_response_1 = MagicMock(status_code=201)
        card_response_1.json.return_value = {"id": 201}
        card_response_2 = MagicMock(status_code=201)
        card_response_2.json.return_value = {"id": 202}
        mock_post.side_effect = [dashboard_response, card_response_1, card_response_2]
        mock_put.return_value = MagicMock(status_code=200)

        with patch.object(dashboard_manager, "get_dashboards", return_value=[]):
            dashboard_data = {
                "name": "Card ID Dashboard",
                "description": "",
                "cards": [
                    {
                        "name": "Card A",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}},
                        "display": "table",
                        "position": {},
                    },
                    {
                        "name": "Card B",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 2"}},
                        "display": "table",
                        "position": {},
                    },
                ],
            }

            result = dashboard_manager._create_dashboard_from_yaml(dashboard_data)

        assert result == {"dashboard_id": 77, "card_ids": [201, 202]}

    @patch("dango.visualization.dashboard_manager.requests.put")
    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_attach_failure_raises_and_returns_none(self, mock_post, mock_put, dashboard_manager):
        """A non-200 from the PUT attach call is loud: caught internally,
        logged, and surfaces as a None return (this file's existing
        failure convention), not silently reported as a successful import
        with zero cards."""
        dashboard_response = MagicMock(status_code=200)
        dashboard_response.json.return_value = {"id": 99}
        card_response = MagicMock(status_code=200)
        card_response.json.return_value = {"id": 11}
        mock_post.side_effect = [dashboard_response, card_response]

        mock_put.return_value = MagicMock(status_code=404, text="Not Found")

        with patch.object(dashboard_manager, "get_dashboards", return_value=[]):
            dashboard_data = {
                "name": "Test Dashboard",
                "description": "desc",
                "cards": [
                    {
                        "name": "Card A",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}},
                        "display": "table",
                        "position": {},
                    },
                ],
            }

            result = dashboard_manager._create_dashboard_from_yaml(dashboard_data)

        # Failure must not be silently swallowed into a "successful" import
        assert result is None

    @patch("dango.visualization.dashboard_manager.requests.put")
    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_no_cards_created_skips_put_call(self, mock_post, mock_put, dashboard_manager):
        """If every card fails to create, there's nothing to attach --
        the PUT call should be skipped rather than sent with an empty
        dashcards array."""
        dashboard_response = MagicMock(status_code=200)
        dashboard_response.json.return_value = {"id": 99}
        failed_card_response = MagicMock(status_code=500, text="error")
        mock_post.side_effect = [dashboard_response, failed_card_response]

        with patch.object(dashboard_manager, "get_dashboards", return_value=[]):
            dashboard_data = {
                "name": "Test Dashboard",
                "description": "desc",
                "cards": [
                    {
                        "name": "Card A",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}},
                        "display": "table",
                        "position": {},
                    },
                ],
            }

            result = dashboard_manager._create_dashboard_from_yaml(dashboard_data)

        assert result == {"dashboard_id": 99, "card_ids": []}
        mock_put.assert_not_called()


class TestLoadFromFilesRollback:
    """Tests for load_from_files()'s rollback-on-failure path (1.0.8-Q10 fix #2).

    Live-reproduced incident (2026-09-09/10): a dashboard with embedded
    cards imports successfully, then a later, unrelated question import
    fails. The rollback must delete the embedded cards too, not just the
    dashboard -- pre-fix, the cards were left behind as orphaned, parent-less
    questions in Metabase while the tool printed a false "Rollback complete
    - no changes applied."
    """

    def _write_yaml(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(data, f)

    @patch("dango.visualization.dashboard_manager.requests.delete")
    @patch("dango.visualization.dashboard_manager.requests.put")
    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_dashboard_rollback_deletes_embedded_cards(
        self, mock_post, mock_put, mock_delete, dashboard_manager, tmp_path
    ):
        """A later import failure must roll back an already-created
        dashboard's embedded cards, not just the dashboard itself."""
        metabase_dir = tmp_path / "metabase"

        self._write_yaml(
            metabase_dir / "dashboards" / "dash1.yml",
            {
                "name": "Test Dashboard",
                "description": "",
                "cards": [
                    {
                        "name": "Card A",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}},
                        "display": "table",
                        "position": {"row": 0, "col": 0, "size_x": 6, "size_y": 4},
                    },
                    {
                        "name": "Card B",
                        "dataset_query": {"type": "native", "native": {"query": "SELECT 2"}},
                        "display": "table",
                        "position": {"row": 0, "col": 6, "size_x": 6, "size_y": 4},
                    },
                ],
            },
        )
        self._write_yaml(
            metabase_dir / "questions" / "bad_question.yml",
            {
                "name": "Bad Question",
                "dataset_query": {"type": "native", "native": {"query": "SELECT * FROM broken"}},
                "display": "table",
            },
        )

        dashboard_response = MagicMock(status_code=200)
        dashboard_response.json.return_value = {"id": 500}
        card_response_1 = MagicMock(status_code=200)
        card_response_1.json.return_value = {"id": 501}
        card_response_2 = MagicMock(status_code=200)
        card_response_2.json.return_value = {"id": 502}
        failed_question_response = MagicMock(status_code=500, text="error")
        mock_post.side_effect = [
            dashboard_response,
            card_response_1,
            card_response_2,
            failed_question_response,
        ]
        mock_put.return_value = MagicMock(status_code=200)
        mock_delete.return_value = MagicMock(status_code=200)

        with (
            patch.object(dashboard_manager, "get_dashboards", return_value=[]),
            patch.object(dashboard_manager, "get_cards", return_value=[]),
        ):
            result = dashboard_manager.load_from_files(overwrite=False, dry_run=False)

        assert result["success"] is False
        assert result["errors"]

        deleted_urls = {call.args[0] for call in mock_delete.call_args_list}
        assert "http://localhost:3000/api/dashboard/500" in deleted_urls
        # The bug: embedded cards were never tracked, so they survived
        # rollback as orphans. Both must now be deleted alongside the
        # dashboard that owned them.
        assert "http://localhost:3000/api/card/501" in deleted_urls
        assert "http://localhost:3000/api/card/502" in deleted_urls
        assert mock_delete.call_count == 3
