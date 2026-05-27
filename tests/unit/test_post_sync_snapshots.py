"""tests/unit/test_post_sync_snapshots.py

Unit tests for BUG-206: Auto-run dbt snapshots after sync.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def project_root(tmp_path):
    """Create a temporary project root with dbt/snapshots directory."""
    return tmp_path


class TestRunDbtSnapshots:
    """Tests for ``_run_dbt_snapshots()``."""

    def test_runs_when_snapshot_files_exist(self, project_root):
        """Snapshots run when .sql files exist in dbt/snapshots/."""
        from dango.utils.post_sync import _run_dbt_snapshots

        snapshot_dir = project_root / "dbt" / "snapshots"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "snap_orders.sql").write_text("SELECT 1")

        with patch(
            "dango.transformation.run_dbt_snapshots",
            return_value=(True, "Success"),
        ) as mock_run:
            _run_dbt_snapshots(project_root)
            mock_run.assert_called_once_with(project_root)

    def test_skips_when_no_snapshot_dir(self, project_root):
        """No error when dbt/snapshots/ doesn't exist."""
        from dango.utils.post_sync import _run_dbt_snapshots

        with patch(
            "dango.transformation.run_dbt_snapshots",
        ) as mock_run:
            _run_dbt_snapshots(project_root)
            mock_run.assert_not_called()

    def test_skips_when_no_sql_files(self, project_root):
        """No error when dbt/snapshots/ exists but has no .sql files."""
        from dango.utils.post_sync import _run_dbt_snapshots

        snapshot_dir = project_root / "dbt" / "snapshots"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "README.md").write_text("Notes")

        with patch(
            "dango.transformation.run_dbt_snapshots",
        ) as mock_run:
            _run_dbt_snapshots(project_root)
            mock_run.assert_not_called()

    def test_failure_logged_not_raised(self, project_root):
        """Snapshot failure is logged but does not propagate."""
        from dango.utils.post_sync import _run_dbt_snapshots

        snapshot_dir = project_root / "dbt" / "snapshots"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "snap_orders.sql").write_text("SELECT 1")

        with patch(
            "dango.transformation.run_dbt_snapshots",
            return_value=(False, "dbt snapshot failed"),
        ):
            # Should not raise
            _run_dbt_snapshots(project_root)

    def test_exception_logged_not_raised(self, project_root):
        """Unexpected exception is logged but does not propagate."""
        from dango.utils.post_sync import _run_dbt_snapshots

        snapshot_dir = project_root / "dbt" / "snapshots"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "snap_orders.sql").write_text("SELECT 1")

        with patch(
            "dango.transformation.run_dbt_snapshots",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            _run_dbt_snapshots(project_root)
