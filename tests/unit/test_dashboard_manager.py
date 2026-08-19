"""tests/unit/test_dashboard_manager.py

Unit tests for dashboard_manager module: _parse_parent_id and _import_collections.
"""

from unittest.mock import patch

import pytest

from dango.visualization.dashboard_manager import DashboardManager


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
        """Single-level collection '/1/' → None (at root)."""
        assert dashboard_manager._parse_parent_id("/1/") is None

    def test_parse_parent_id_nested(self, dashboard_manager):
        """Nested collection '/1/5/' → parent 1."""
        assert dashboard_manager._parse_parent_id("/1/5/") == 1

    def test_parse_parent_id_deep_nested(self, dashboard_manager):
        """Deep nested '/1/5/7/' → parent 5."""
        assert dashboard_manager._parse_parent_id("/1/5/7/") == 5

    def test_parse_parent_id_very_deep(self, dashboard_manager):
        """Very deep '/1/2/3/4/5/' → parent 4."""
        assert dashboard_manager._parse_parent_id("/1/2/3/4/5/") == 4

    def test_parse_parent_id_no_leading_slash(self, dashboard_manager):
        """Location without leading/trailing slashes."""
        assert dashboard_manager._parse_parent_id("1/5") == 1

    def test_parse_parent_id_empty_string(self, dashboard_manager):
        """Empty string → None."""
        assert dashboard_manager._parse_parent_id("") is None


class TestImportCollections:
    """Tests for _import_collections method."""

    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_import_collections_topological_order(self, mock_post, dashboard_manager):
        """Collections are created in topological order (root first)."""
        # Mock the API responses
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.side_effect = [
            {"id": 100},  # first root collection
            {"id": 101},  # second root collection
            {"id": 102},  # child (has parent in mapped IDs)
        ]

        # Collections: two roots and one child
        collections = [
            {"id": 2, "name": "Root2", "parent_id": None},
            {"id": 1, "name": "Root1", "parent_id": None},
            {"id": 3, "name": "Child", "parent_id": 2},
        ]

        id_mapping = dashboard_manager._import_collections(collections)

        # Check that both roots were created first
        assert mock_post.call_count >= 3
        calls = mock_post.call_args_list

        # First two calls should be for root collections (parent_id=None)
        first_call_payload = calls[0].kwargs.get("json")
        second_call_payload = calls[1].kwargs.get("json")
        assert first_call_payload["parent_id"] is None
        assert second_call_payload["parent_id"] is None

        # Third call should be for child with mapped parent ID
        third_call_payload = calls[2].kwargs.get("json")
        assert third_call_payload["parent_id"] in (100, 101)  # One of the created roots

        # Verify all collections are in mapping
        assert 1 in id_mapping
        assert 2 in id_mapping
        assert 3 in id_mapping
        # Child should be mapped to a new ID
        assert id_mapping[3] == 102

    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_import_collections_id_mapping(self, mock_post, dashboard_manager):
        """ID mapping maps old IDs to new IDs correctly."""
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.side_effect = [
            {"id": 10},
            {"id": 20},
        ]

        collections = [
            {"id": 1, "name": "First", "parent_id": None},
            {"id": 2, "name": "Second", "parent_id": 1},
        ]

        id_mapping = dashboard_manager._import_collections(collections)

        # Old ID 1 maps to new ID 10
        assert id_mapping.get(1) == 10
        # Old ID 2 maps to new ID 20
        assert id_mapping.get(2) == 20

    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_import_collections_empty_list(self, mock_post, dashboard_manager):
        """Empty collections list returns empty mapping."""
        id_mapping = dashboard_manager._import_collections([])
        assert id_mapping == {}
        mock_post.assert_not_called()

    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_import_collections_api_error_fallback(self, mock_post, dashboard_manager):
        """On API error, fallback to get_collection_id."""
        # First call fails, second call (fallback) succeeds
        mock_post.return_value.status_code = 500

        with patch.object(dashboard_manager, "get_collection_id", return_value=99) as mock_get_id:
            collections = [{"id": 1, "name": "Test", "parent_id": None}]
            id_mapping = dashboard_manager._import_collections(collections)

            # Fallback should have been called
            mock_get_id.assert_called_with("Test")
            # ID mapping should use fallback result
            assert id_mapping.get(1) == 99

    @patch("dango.visualization.dashboard_manager.requests.post")
    def test_import_collections_broken_ref(self, mock_post, dashboard_manager):
        """Broken parent references are handled gracefully."""
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.side_effect = [
            {"id": 100},
            {"id": 101},  # created even though parent doesn't exist
        ]

        collections = [
            {"id": 1, "name": "Root", "parent_id": None},
            {"id": 2, "name": "Orphan", "parent_id": 999},  # non-existent parent
        ]

        id_mapping = dashboard_manager._import_collections(collections)

        # Both should be created (Orphan uses None parent as fallback)
        assert 1 in id_mapping
        assert 2 in id_mapping
