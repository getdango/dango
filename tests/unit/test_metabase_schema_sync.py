"""tests/unit/test_metabase_schema_sync.py

Unit tests for sync_metabase_schema()'s re-sync completion poll
in dango/visualization/metabase.py.

Split out from test_metabase_setup.py (which covers the rest of
sync_metabase_schema()'s behavior) to keep both files under the
project's 500-line-per-file limit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _empty_task_resp() -> MagicMock:
    """Mock GET /api/task response with no prior tasks (used as poll baseline)."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    return resp


def _task_resp(status: str, task_id: int = 1, db_id: int = 5) -> MagicMock:
    """Mock GET /api/task response containing one "sync" task with the given status."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "data": [{"id": task_id, "task": "sync", "status": status, "db_id": db_id}]
    }
    return resp


@pytest.mark.unit
class TestSyncMetabaseSchemaTaskPoll:
    """Regression tests for the initial_sync_status completion-check bug.

    initial_sync_status only reflects a database's one-time initial
    onboarding sync, so it was already "complete" on the first poll for
    any re-sync — these tests confirm the fix instead polls GET /api/task
    for the specific "sync" task the sync_schema call triggered.
    """

    def test_waits_for_sync_task_to_finish(self, tmp_path: Path) -> None:
        """The poll must keep checking GET /api/task across multiple
        iterations while the triggered "sync" task is still "started",
        not exit on the first poll."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            yaml.dump({"admin": {"email": "a@test.com", "password": "s"}, "database": {"id": 5}})
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }

        mock_session.post.side_effect = [login_resp, sync_resp]
        # Baseline, then "started" x2 (must keep polling), then "success".
        mock_session.get.side_effect = [
            _empty_task_resp(),
            _task_resp("started"),
            _task_resp("started"),
            _task_resp("success"),
            metadata_resp,
        ]
        mock_session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep") as mock_sleep,
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is True
        assert mock_sleep.call_count == 3  # kept polling past the first "started" check
        assert mock_session.get.call_count == 5  # baseline + 3 polls + metadata

    def test_times_out_gracefully_when_task_never_completes(self, tmp_path: Path) -> None:
        """If the sync task never leaves 'started', return without raising
        (matches existing timeout-tolerant behavior)."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            yaml.dump({"admin": {"email": "a@test.com", "password": "s"}, "database": {"id": 5}})
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }

        mock_session.post.side_effect = [login_resp, sync_resp]
        # Task stays "started" for all 30 poll iterations — never settles.
        mock_session.get.side_effect = [
            _empty_task_resp(),
            *([_task_resp("started")] * 30),
            metadata_resp,
        ]
        mock_session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep") as mock_sleep,
        ):
            result = sync_metabase_schema(tmp_path)

        # Doesn't raise; falls through to fetch metadata after exhausting poll budget.
        assert result is True
        assert mock_sleep.call_count == 30

    def test_falls_back_to_fixed_wait_when_baseline_fetch_fails(self, tmp_path: Path) -> None:
        """If the pre-trigger baseline GET /api/task call fails, the function
        must not silently default to task id 0 — that would let it mistake
        an older, already-finished "sync" task for the one just triggered
        (the same false-positive failure mode this fix exists to remove).
        Instead it falls back to a fixed wait, with no task-status polling."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            yaml.dump({"admin": {"email": "a@test.com", "password": "s"}, "database": {"id": 5}})
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        baseline_fail_resp = MagicMock(status_code=500)
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }

        mock_session.post.side_effect = [login_resp, sync_resp]
        # Baseline fetch fails (500) -> no task-status poll happens at all.
        mock_session.get.side_effect = [baseline_fail_resp, metadata_resp]
        mock_session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep") as mock_sleep,
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is True
        mock_sleep.assert_called_once_with(5)  # fixed fallback wait, not the poll loop
        assert mock_session.get.call_count == 2  # baseline attempt + metadata (no polling)

    def test_logs_warning_when_sync_task_fails(self, tmp_path: Path) -> None:
        """If the triggered sync task ends in a non-success terminal status
        (e.g. "failed"), the poll must stop rather than retry forever, but
        must log a warning instead of silently treating it as a success."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            yaml.dump({"admin": {"email": "a@test.com", "password": "s"}, "database": {"id": 5}})
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [_empty_task_resp(), _task_resp("failed"), metadata_resp]
        mock_session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
            patch("dango.visualization.metabase.logger") as mock_logger,
        ):
            result = sync_metabase_schema(tmp_path)

        # Still completes (doesn't raise, doesn't retry forever) — but the
        # failure is now observable instead of silent.
        assert result is True
        warning_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
        assert any("unexpected status" in msg for msg in warning_msgs)

    def test_sync_metabase_schema_task_poll_order_documented(self, tmp_path: Path) -> None:
        """1.0.8-AD: live-verified against a real Metabase v0.62.18 instance with 27
        historical "sync" task records for one database — GET /api/task's default
        (no sort params passed) order is reliably newest-first by id, and an explicit
        sort_column=started_at&sort_direction=desc call produces byte-identical
        ordering to the unsorted default (confirming this isn't coincidental — it's
        Metabase's own SortParams schema default). See
        v1.0.x-planning/1.0.8/BUGS-FOUND.md's "task-list poll assumes newest-first
        ordering" entry for the full raw-response evidence.

        This test does NOT assert on response order — it deliberately mocks the
        limit=20 poll response in non-newest-first (scrambled) order to prove the
        baseline-ID/max-by-id selection logic (not response order) is what makes
        the poll safe. Even if a future Metabase version changed its default
        ordering, this selection logic would still correctly find the triggered
        task as long as it appears anywhere in the returned window.
        """
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            yaml.dump({"admin": {"email": "a@test.com", "password": "s"}, "database": {"id": 5}})
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }

        # Baseline: 3 prior "sync" tasks already exist (ids 100, 101, 102) — the
        # real baseline_task_id becomes max(id)=102.
        baseline_resp = MagicMock(status_code=200)
        baseline_resp.json.return_value = {
            "data": [
                {"id": 100, "task": "sync", "status": "success"},
                {"id": 101, "task": "sync", "status": "success"},
                {"id": 102, "task": "sync", "status": "success"},
            ]
        }

        # Poll response: deliberately NOT newest-first (scrambled order) — the
        # real just-triggered task (id=103) is buried in the middle, with an
        # older, already-finished unrelated task (id=99) mixed in too. If the
        # code trusted response order (e.g. "the first sync task is the right
        # one"), it would pick the wrong task here. It must instead filter to
        # id > baseline_task_id (102) and take the max by id.
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {
            "data": [
                {"id": 99, "task": "sync", "status": "success"},
                {"id": 103, "task": "sync", "status": "success"},
                {"id": 101, "task": "sync", "status": "success"},
                {"id": 100, "task": "sync", "status": "success"},
                {"id": 102, "task": "sync", "status": "success"},
            ]
        }

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [baseline_resp, poll_resp, metadata_resp]
        mock_session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path)

        # Correctly identified task id=103 (the only one > baseline 102) as the
        # triggered task and exited the poll on the first iteration (status
        # already "success") — proving max-by-id, not response position/order,
        # drove the decision.
        assert result is True
        assert mock_session.get.call_count == 3  # baseline + 1 poll + metadata


@pytest.mark.unit
class TestSyncMetabaseSchemaSessionReuse:
    """1.0.8-Q17: sync_metabase_schema()'s optional `existing_session_id` --
    lets a caller that already has a valid Metabase session token (e.g.
    refresh_metabase_connection()'s own post-restart login) skip this
    function's own _metabase_login() call. Metabase auth is header-based
    (X-Metabase-Session), not cookie-based, so a token from one
    requests.Session object is valid on requests made via another."""

    def test_existing_session_id_skips_own_login(self, tmp_path: Path) -> None:
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        # No login response queued -- if sync_metabase_schema tried its own
        # login, the next .post() call (sync_schema) would consume this
        # response as if it were the login response, corrupting the flow.
        sync_resp = MagicMock(status_code=200)
        mock_session.post.return_value = sync_resp
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }
        mock_session.get.side_effect = [
            _empty_task_resp(),
            _task_resp("success"),
            metadata_resp,
        ]

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path, existing_session_id="sess-reused")

        assert result is True
        # Exactly one post call: the sync_schema trigger. No login POST at all.
        assert mock_session.post.call_count == 1
        assert (
            mock_session.post.call_args_list[0][0][0]
            == "http://localhost:3000/api/database/5/sync_schema"
        )
        # The reused token, not a freshly-logged-in one, is what's sent.
        assert (
            mock_session.post.call_args_list[0][1]["headers"]["X-Metabase-Session"] == "sess-reused"
        )

    def test_falsy_existing_session_id_falls_back_to_own_login(self, tmp_path: Path) -> None:
        """An explicit falsy existing_session_id (empty string) is treated the
        same as not passing one at all -- still logs in for itself."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=401)
        mock_session.post.return_value = login_resp

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            result = sync_metabase_schema(tmp_path, existing_session_id="")

        assert result is False
        # Own login was attempted (and failed) -- proves the falsy value wasn't
        # treated as a usable token.
        mock_session.post.assert_called_once_with(
            "http://localhost:3000/api/session",
            json={"username": "admin@test.com", "password": "secret"},
            timeout=10,
        )


class TestSyncMetabaseSchemaUrlResolution:
    """1.0.8-fix: sync_metabase_schema() must resolve a caller-omitted
    metabase_url from .dango/metabase.yml's own "metabase_url" key (same
    precedent as set_metabase_telemetry()) instead of always defaulting to
    localhost:3000 -- any project on a non-default platform.metabase_port
    had every automatic post-sync schema refresh silently target the wrong
    host. See BUGS-FOUND.md."""

    def test_uses_configured_url_from_creds_file_when_not_passed(self, tmp_path: Path) -> None:
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "metabase_url": "http://localhost:13000",
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-1"}
        sync_resp = MagicMock(status_code=200)
        mock_session.post.side_effect = [login_resp, sync_resp]
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [{"id": 1, "name": "stg_orders", "schema": "staging"}]
        }
        mock_session.get.side_effect = [
            _empty_task_resp(),
            _task_resp("success"),
            metadata_resp,
        ]

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is True
        # Every real call went to the configured port, not the hardcoded default.
        assert mock_session.post.call_args_list[0][0][0] == "http://localhost:13000/api/session"
        assert (
            mock_session.post.call_args_list[1][0][0]
            == "http://localhost:13000/api/database/5/sync_schema"
        )

    def test_explicit_metabase_url_wins_over_creds_file(self, tmp_path: Path) -> None:
        """An explicitly-passed metabase_url still takes priority over the
        creds file's own value -- e.g. cli/commands/metabase_cmd.py already
        resolves and passes it itself."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "metabase_url": "http://localhost:13000",
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=401)
        mock_session.post.return_value = login_resp

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            sync_metabase_schema(tmp_path, metabase_url="http://localhost:19999")

        assert mock_session.post.call_args_list[0][0][0] == "http://localhost:19999/api/session"
