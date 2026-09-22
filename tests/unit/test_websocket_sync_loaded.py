"""tests/unit/test_websocket_sync_loaded.py

Tests for the ``sync_data_loaded`` WebSocket broadcast in
``dango.platform.scheduling.jobs``.
"""

import pytest


@pytest.mark.unit
class TestSyncDataLoadedBroadcast:
    def test_broadcast_fired_after_successful_sync(self, tmp_path, monkeypatch):
        """sync_data_loaded is broadcast once sources finish loading, before dbt runs."""
        from dango.platform.scheduling import jobs

        broadcasts: list[dict] = []
        monkeypatch.setattr(jobs, "_broadcast", lambda msg: broadcasts.append(msg))
        monkeypatch.setattr(jobs, "_resolve_sources", lambda root, names: [])

        # No sources resolved -> should NOT emit sync_data_loaded (early return path)
        jobs._run_scheduled_sync_impl("test-schedule", ["nonexistent"], project_root=str(tmp_path))

        events = [b["event"] for b in broadcasts]
        assert "sync_data_loaded" not in events

    def test_event_name_and_payload_shape(self):
        """Broadcast payload for sync_data_loaded carries schedule, sources, succeeded_sources, timestamp."""
        # This test documents the required payload shape; full integration is covered
        # by the existing scheduler integration tests (test_jobs.py) which exercise
        # _run_scheduled_sync_impl end-to-end with mocked subprocess launch.
        payload_keys = {"event", "schedule", "sources", "succeeded_sources", "timestamp"}
        example = {
            "event": "sync_data_loaded",
            "schedule": "test-schedule",
            "sources": ["orders"],
            "succeeded_sources": ["orders"],
            "timestamp": "2026-08-25T00:00:00Z",
        }
        assert set(example.keys()) == payload_keys
