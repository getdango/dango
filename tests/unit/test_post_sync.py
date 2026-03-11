"""tests/unit/test_post_sync.py

Tests for the post-sync dispatcher (dango/utils/post_sync.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.utils.post_sync import dispatch_post_sync_hooks


@pytest.mark.unit
class TestDispatchPostSyncHooks:
    """Unit tests for dispatch_post_sync_hooks."""

    def test_calls_all_hooks(self, tmp_path: Path):
        """All four hooks are called with the correct sources."""
        sources = ["shopify", "stripe"]
        with (
            patch("dango.utils.post_sync._run_profiling") as mock_prof,
            patch("dango.utils.post_sync._run_drift_detection") as mock_drift,
            patch("dango.utils.post_sync._run_pii_scan") as mock_pii,
            patch("dango.utils.post_sync._run_analysis") as mock_analysis,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=sources)

        mock_prof.assert_called_once_with(tmp_path, sources)
        mock_drift.assert_called_once_with(tmp_path, sources)
        mock_pii.assert_called_once_with(tmp_path, sources)
        mock_analysis.assert_called_once_with(tmp_path, sources)

    def test_empty_sources_skips_hooks(self, tmp_path: Path):
        """No hooks are called when sources list is empty."""
        with patch("dango.utils.post_sync._run_profiling") as mock_prof:
            dispatch_post_sync_hooks(project_root=tmp_path, sources=[])

        mock_prof.assert_not_called()

    def test_stubs_are_noop(self, tmp_path: Path):
        """Calling with real stubs does not raise."""
        dispatch_post_sync_hooks(project_root=tmp_path, sources=["test_source"])

    def test_analysis_hook_passes_raw_prefix(self, tmp_path: Path) -> None:
        """Analysis hook prepends raw_ to source names for source_filter."""
        with patch(
            "dango.analysis.metrics.run_analysis",
            return_value=[],
        ) as mock_engine:
            from dango.utils.post_sync import _run_analysis

            _run_analysis(tmp_path, ["stripe", "ga"])

        mock_engine.assert_called_once_with(tmp_path, source_filter=["raw_stripe", "raw_ga"])

    def test_profiling_engine_error_isolation(self, tmp_path: Path) -> None:
        """When profiling engine raises, _run_profiling catches it and drift/pii/analysis still run."""
        with (
            patch("dango.utils.post_sync.profile_table", side_effect=RuntimeError("boom")),
            patch("dango.governance.schema_drift.detect_drift_for_sources") as mock_drift,
            patch("dango.governance.pii_detector.scan_sources_for_pii") as mock_pii,
            patch("dango.analysis.metrics.run_analysis", return_value=[]) as mock_analysis,
        ):
            # Create minimal warehouse so _run_profiling tries to profile
            db_path = tmp_path / "data" / "warehouse.duckdb"
            db_path.parent.mkdir(parents=True)

            import duckdb

            conn = duckdb.connect(str(db_path))
            conn.execute("CREATE SCHEMA raw_testshop")
            conn.execute("CREATE TABLE raw_testshop.orders (id INTEGER)")
            conn.close()

            dispatch_post_sync_hooks(project_root=tmp_path, sources=["testshop"])

        # Drift, PII, and analysis still called despite profiling failure
        mock_drift.assert_called_once()
        mock_pii.assert_called_once()
        mock_analysis.assert_called_once()

    def test_drift_engine_error_isolation(self, tmp_path: Path) -> None:
        """When drift engine raises, _run_drift_detection catches it and pii/analysis still run."""
        with (
            patch("dango.utils.post_sync._run_profiling"),
            patch(
                "dango.governance.schema_drift.detect_drift_for_sources",
                side_effect=RuntimeError("drift boom"),
            ),
            patch("dango.governance.pii_detector.scan_sources_for_pii") as mock_pii,
            patch("dango.analysis.metrics.run_analysis", return_value=[]) as mock_analysis,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        mock_pii.assert_called_once()
        mock_analysis.assert_called_once()

    def test_pii_engine_error_isolation(self, tmp_path: Path) -> None:
        """When PII engine raises, _run_pii_scan catches it and analysis still runs."""
        with (
            patch("dango.utils.post_sync._run_profiling"),
            patch("dango.utils.post_sync._run_drift_detection"),
            patch(
                "dango.governance.pii_detector.scan_sources_for_pii",
                side_effect=RuntimeError("pii boom"),
            ),
            patch("dango.analysis.metrics.run_analysis", return_value=[]) as mock_analysis,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        mock_analysis.assert_called_once()

    def test_hooks_called_in_order(self, tmp_path: Path) -> None:
        """Hooks are called in order: profiling -> drift -> pii -> analysis."""
        call_order: list[str] = []

        def _make_side_effect(name: str):
            def _side_effect(*_args, **_kwargs):
                call_order.append(name)

            return _side_effect

        with (
            patch(
                "dango.utils.post_sync._run_profiling",
                side_effect=_make_side_effect("profiling"),
            ),
            patch(
                "dango.utils.post_sync._run_drift_detection",
                side_effect=_make_side_effect("drift"),
            ),
            patch(
                "dango.utils.post_sync._run_pii_scan",
                side_effect=_make_side_effect("pii"),
            ),
            patch(
                "dango.utils.post_sync._run_analysis",
                side_effect=_make_side_effect("analysis"),
            ),
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        assert call_order == ["profiling", "drift", "pii", "analysis"]

    def test_log_start_and_complete(self, tmp_path: Path) -> None:
        """Verify logger.info called with post_sync_hooks_start and _complete."""
        with (
            patch("dango.utils.post_sync._run_profiling"),
            patch("dango.utils.post_sync._run_drift_detection"),
            patch("dango.utils.post_sync._run_pii_scan"),
            patch("dango.utils.post_sync._run_analysis"),
            patch("dango.utils.post_sync.logger") as mock_logger,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        info_calls = list(mock_logger.info.call_args_list)
        events = [c[0][0] for c in info_calls]
        assert "post_sync_hooks_start" in events
        assert "post_sync_hooks_complete" in events

    def test_drift_hook_calls_engine(self, tmp_path: Path) -> None:
        """Call _run_drift_detection directly and verify it calls the engine."""
        with patch("dango.governance.schema_drift.detect_drift_for_sources") as mock_engine:
            from dango.utils.post_sync import _run_drift_detection

            _run_drift_detection(tmp_path, ["shopify", "stripe"])

        mock_engine.assert_called_once_with(tmp_path, ["shopify", "stripe"])
