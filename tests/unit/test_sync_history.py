"""tests/unit/test_sync_history.py

Tests for dango.utils.sync_history — update_last_sync_entry().
"""

import json

import pytest


@pytest.mark.unit
class TestUpdateLastSyncEntry:
    """Test update_last_sync_entry() for adding fields to the most recent entry."""

    def test_adds_field_to_last_entry(self, tmp_path):
        """Should add transform_error to the most recent history entry."""
        from dango.utils.sync_history import (
            save_sync_history_entry,
            update_last_sync_entry,
        )

        save_sync_history_entry(tmp_path, "my_source", {"status": "success", "rows": 100})
        save_sync_history_entry(tmp_path, "my_source", {"status": "success", "rows": 200})

        update_last_sync_entry(tmp_path, "my_source", {"transform_error": "dbt failed"})

        history_file = tmp_path / ".dango" / "history" / "my_source.json"
        with open(history_file) as f:
            history = json.load(f)

        # Only the last entry should have transform_error
        assert "transform_error" not in history[0]
        assert history[1]["transform_error"] == "dbt failed"
        # Original fields preserved
        assert history[1]["rows"] == 200

    def test_no_crash_when_file_missing(self, tmp_path):
        """Should silently return when history file doesn't exist."""
        from dango.utils.sync_history import update_last_sync_entry

        # Should not raise
        update_last_sync_entry(tmp_path, "nonexistent", {"transform_error": "dbt failed"})

    def test_no_crash_on_empty_history(self, tmp_path):
        """Should handle empty history list gracefully."""
        from dango.utils.sync_history import get_sync_history_file, update_last_sync_entry

        history_file = get_sync_history_file(tmp_path, "empty_source")
        with open(history_file, "w") as f:
            json.dump([], f)

        # Should not raise
        update_last_sync_entry(tmp_path, "empty_source", {"transform_error": "dbt failed"})

        with open(history_file) as f:
            history = json.load(f)
        assert history == []

    def test_preserves_existing_fields(self, tmp_path):
        """Existing fields in the entry should not be removed."""
        from dango.utils.sync_history import (
            save_sync_history_entry,
            update_last_sync_entry,
        )

        save_sync_history_entry(tmp_path, "src", {"status": "success", "rows": 50, "duration": 3.5})

        update_last_sync_entry(tmp_path, "src", {"transform_error": "column missing"})

        history_file = tmp_path / ".dango" / "history" / "src.json"
        with open(history_file) as f:
            history = json.load(f)

        entry = history[0]
        assert entry["status"] == "success"
        assert entry["rows"] == 50
        assert entry["duration"] == 3.5
        assert entry["transform_error"] == "column missing"
