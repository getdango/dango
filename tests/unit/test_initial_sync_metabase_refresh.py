"""tests/unit/test_initial_sync_metabase_refresh.py

1.0.8-AC: Unit tests for web/routes/initial_sync.py's Metabase refresh call
site. Split out of test_initial_sync.py (already at the 500-line file-size
limit) rather than extended in place -- these tests don't need that file's
FastAPI app/client fixtures, only the module's _refresh_metabase() and
_run_initial_sync() functions.

Covers two fixes combined in one call site (BUGS-FOUND.md):
1. _refresh_metabase() blocked the event loop -- now dispatched via
   asyncio.to_thread().
2. sync_metabase_schema() could read a stale DuckDB connection --
   _refresh_metabase() now calls refresh_metabase_connection() first and
   threads existing_session_id through, matching dlt_runner.py/jobs.py/
   transform.py's already-fixed callers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from dango.web.routes.initial_sync import InitialSyncState, _refresh_metabase


@pytest.mark.unit
class TestMetabaseRefreshDispatch:
    @patch("dango.web.routes.initial_sync._save_and_broadcast", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._sync_single_source", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._generate_dbt_docs")
    @patch("asyncio.to_thread", new_callable=AsyncMock)
    @pytest.mark.anyio
    async def test_runs_in_thread_not_on_event_loop(
        self, mock_to_thread, mock_dbt, mock_sync, mock_broadcast, tmp_path
    ):
        """_refresh_metabase() must be dispatched via asyncio.to_thread(),
        not awaited directly, since it blocks for a container restart."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state = InitialSyncState()
        with patch("dango.web.routes.initial_sync._check_disk_usage", return_value=None):
            await mod._run_initial_sync(tmp_path, [{"name": "hubspot", "type": "hubspot"}])

        mock_to_thread.assert_called_once_with(mod._refresh_metabase, tmp_path)


@pytest.mark.unit
class TestRefreshMetabaseStaleness:
    def test_calls_refresh_connection_before_schema_sync(self, tmp_path):
        order: list[str] = []

        def _refresh(project_root):
            order.append("refresh")
            return True, None, "sess-abc"

        def _sync(project_root, existing_session_id=None):
            order.append("sync")
            assert existing_session_id == "sess-abc"
            return True

        with (
            patch("dango.visualization.metabase.refresh_metabase_connection", side_effect=_refresh),
            patch("dango.visualization.metabase.sync_metabase_schema", side_effect=_sync),
        ):
            _refresh_metabase(tmp_path)

        assert order == ["refresh", "sync"]

    def test_skips_schema_sync_and_swallows_errors_when_refresh_fails(self, tmp_path):
        """No sync attempt through a known-stale connection, and the
        existing try/except still swallows the failure (no raise)."""
        with (
            patch(
                "dango.visualization.metabase.refresh_metabase_connection",
                side_effect=RuntimeError("boom"),
            ),
            patch("dango.visualization.metabase.sync_metabase_schema") as mock_sync,
        ):
            _refresh_metabase(tmp_path)  # must not raise

        mock_sync.assert_not_called()
