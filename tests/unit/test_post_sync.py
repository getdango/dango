"""tests/unit/test_post_sync.py

Tests for the post-sync dispatcher (dango/utils/post_sync.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.utils.post_sync import dispatch_post_sync_hooks


@pytest.mark.unit
class TestDispatchPostSyncHooks:
    """Unit tests for dispatch_post_sync_hooks."""

    def test_calls_all_hooks(self, tmp_path: Path):
        """All three hooks are called with the correct sources (drift moved to pre-dbt)."""
        sources = ["shopify", "stripe"]
        with (
            patch("dango.utils.post_sync._run_profiling") as mock_prof,
            patch("dango.utils.post_sync._run_pii_scan") as mock_pii,
            patch("dango.utils.post_sync._run_analysis") as mock_analysis,
        ):
            result = dispatch_post_sync_hooks(project_root=tmp_path, sources=sources)

        mock_prof.assert_called_once_with(tmp_path, sources)
        mock_pii.assert_called_once_with(tmp_path, sources)
        mock_analysis.assert_called_once_with(tmp_path, sources)
        assert result == {"failed_hooks": []}

    def test_empty_sources_skips_hooks(self, tmp_path: Path):
        """No hooks are called when sources list is empty."""
        with patch("dango.utils.post_sync._run_profiling") as mock_prof:
            result = dispatch_post_sync_hooks(project_root=tmp_path, sources=[])

        mock_prof.assert_not_called()
        assert result == {"failed_hooks": []}

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
        """When profiling engine raises, _run_profiling catches it and pii/analysis still run."""
        with (
            patch("dango.utils.post_sync.profile_table", side_effect=RuntimeError("boom")),
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

        # PII and analysis still called despite profiling failure
        mock_pii.assert_called_once()
        mock_analysis.assert_called_once()

    def test_pii_engine_error_isolation(self, tmp_path: Path) -> None:
        """When PII engine raises, _run_pii_scan catches it and analysis still runs."""
        with (
            patch("dango.utils.post_sync._run_profiling"),
            patch(
                "dango.governance.pii_detector.scan_sources_for_pii",
                side_effect=RuntimeError("pii boom"),
            ),
            patch("dango.analysis.metrics.run_analysis", return_value=[]) as mock_analysis,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        mock_analysis.assert_called_once()

    def test_hooks_called_in_order(self, tmp_path: Path) -> None:
        """Hooks are called in order: profiling -> pii -> analysis (drift moved to pre-dbt)."""
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
                "dango.utils.post_sync._run_pii_scan",
                side_effect=_make_side_effect("pii"),
            ),
            patch(
                "dango.utils.post_sync._run_analysis",
                side_effect=_make_side_effect("analysis"),
            ),
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        assert call_order == ["profiling", "pii", "analysis"]

    def test_log_start_and_complete(self, tmp_path: Path) -> None:
        """Verify logger.info called with post_sync_hooks_start and _complete."""
        with (
            patch("dango.utils.post_sync._run_profiling"),
            patch("dango.utils.post_sync._run_pii_scan"),
            patch("dango.utils.post_sync._run_analysis"),
            patch("dango.utils.post_sync.logger") as mock_logger,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        info_calls = list(mock_logger.info.call_args_list)
        events = [c[0][0] for c in info_calls]
        assert "post_sync_hooks_start" in events
        assert "post_sync_hooks_complete" in events

    def test_failed_hooks_returned(self, tmp_path: Path) -> None:
        """When hooks raise, their names appear in failed_hooks."""
        with (
            patch("dango.utils.post_sync._run_profiling", side_effect=RuntimeError("boom")),
            patch("dango.utils.post_sync._enrich_staging_tests"),
            patch("dango.utils.post_sync._run_pii_scan", side_effect=RuntimeError("pii fail")),
            patch("dango.utils.post_sync._run_analysis"),
            patch("dango.utils.post_sync._run_dbt_snapshots"),
        ):
            result = dispatch_post_sync_hooks(project_root=tmp_path, sources=["s1"])

        assert "profiling" in result["failed_hooks"]
        assert "pii_scan" in result["failed_hooks"]
        assert len(result["failed_hooks"]) == 2

    def test_drift_detection_removed_from_post_sync(self, tmp_path: Path) -> None:
        """_run_drift_detection was removed (drift now runs pre-dbt in dlt_runner)."""
        import dango.utils.post_sync as ps

        assert not hasattr(ps, "_run_drift_detection")


# Patch at source modules because _ensure_default_metrics uses lazy imports
# inside the function body (fresh `from X import Y` each call).
_CFG = "dango.config"
_ANALYSIS_CFG = "dango.analysis.config"
_ANALYSIS_TPL = "dango.analysis.templates"


def _make_source(name: str, source_type: str) -> MagicMock:
    """Create a mock DataSource with name and type."""
    src = MagicMock()
    src.name = name
    src.type.value = source_type
    return src


@pytest.mark.unit
class TestEnsureDefaultMetrics:
    """Unit tests for _ensure_default_metrics (BUG-174)."""

    def test_non_ga4_source_triggers_generate(self, tmp_path: Path) -> None:
        """Non-GA4 source (hubspot) triggers generate_metrics_for_source with correct type."""
        from dango.utils.post_sync import _ensure_default_metrics

        mock_config = MagicMock()
        mock_config.sources.sources = [_make_source("hs", "hubspot")]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = []

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(f"{_ANALYSIS_TPL}.generate_metrics_for_source", return_value=[]) as mock_gen,
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config"),
        ):
            _ensure_default_metrics(tmp_path, ["hs"])

        mock_gen.assert_called_once_with("hubspot", "hs", project_root=tmp_path)

    def test_ga4_source_still_passes_google_analytics(self, tmp_path: Path) -> None:
        """GA4 source passes 'google_analytics' type (no regression)."""
        from dango.utils.post_sync import _ensure_default_metrics

        mock_config = MagicMock()
        mock_config.sources.sources = [_make_source("ga", "google_analytics")]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = []

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(f"{_ANALYSIS_TPL}.generate_metrics_for_source", return_value=[]) as mock_gen,
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config"),
        ):
            _ensure_default_metrics(tmp_path, ["ga"])

        mock_gen.assert_called_once_with("google_analytics", "ga", project_root=tmp_path)

    def test_multiple_sources_each_processed(self, tmp_path: Path) -> None:
        """Multiple sources in single sync each get processed."""
        from dango.utils.post_sync import _ensure_default_metrics

        mock_config = MagicMock()
        mock_config.sources.sources = [
            _make_source("hs", "hubspot"),
            _make_source("stripe1", "stripe"),
        ]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = []

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(f"{_ANALYSIS_TPL}.generate_metrics_for_source", return_value=[]) as mock_gen,
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config"),
        ):
            _ensure_default_metrics(tmp_path, ["hs", "stripe1"])

        assert mock_gen.call_count == 2

    def test_dedup_against_existing_monitors(self, tmp_path: Path) -> None:
        """Monitors already in config are not re-added."""
        from dango.analysis.models import MonitorConfig
        from dango.utils.post_sync import _ensure_default_metrics

        existing_monitor = MagicMock()
        existing_monitor.name = "hs_contacts_row_count"

        mock_config = MagicMock()
        mock_config.sources.sources = [_make_source("hs", "hubspot")]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = [existing_monitor]

        new_metric = MonitorConfig(
            name="hs_deals_row_count",
            source_table="raw_hs.deals",
            value_expression="COUNT(*)",
        )
        dup_metric = MonitorConfig(
            name="hs_contacts_row_count",
            source_table="raw_hs.contacts",
            value_expression="COUNT(*)",
        )

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(
                f"{_ANALYSIS_TPL}.generate_metrics_for_source",
                return_value=[new_metric, dup_metric],
            ),
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config") as mock_add,
        ):
            _ensure_default_metrics(tmp_path, ["hs"])

        # Only the non-duplicate metric should be added
        mock_add.assert_called_once()
        assert mock_add.call_args[0][0] == tmp_path  # project_root passed correctly
        added = mock_add.call_args[0][1]
        assert len(added) == 1
        assert added[0].name == "hs_deals_row_count"

    def test_never_raises(self, tmp_path: Path) -> None:
        """Function never raises — catches exceptions."""
        from dango.utils.post_sync import _ensure_default_metrics

        with patch(f"{_CFG}.get_config", side_effect=RuntimeError("boom")):
            # Should not raise
            _ensure_default_metrics(tmp_path, ["hs"])

    def test_non_synced_sources_skipped(self, tmp_path: Path) -> None:
        """Only synced sources are processed (non-synced sources skipped)."""
        from dango.utils.post_sync import _ensure_default_metrics

        mock_config = MagicMock()
        mock_config.sources.sources = [
            _make_source("hs", "hubspot"),
            _make_source("other", "stripe"),
        ]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = []

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(f"{_ANALYSIS_TPL}.generate_metrics_for_source", return_value=[]) as mock_gen,
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config"),
        ):
            # Only "hs" was synced, not "other"
            _ensure_default_metrics(tmp_path, ["hs"])

        mock_gen.assert_called_once_with("hubspot", "hs", project_root=tmp_path)

    def test_cross_source_dedup(self, tmp_path: Path) -> None:
        """Monitors added by source A are not duplicated by source B."""
        from dango.analysis.models import MonitorConfig
        from dango.utils.post_sync import _ensure_default_metrics

        mock_config = MagicMock()
        mock_config.sources.sources = [
            _make_source("src_a", "hubspot"),
            _make_source("src_b", "stripe"),
        ]
        mock_monitors_config = MagicMock()
        mock_monitors_config.monitors = []

        shared_metric = MonitorConfig(
            name="shared_metric",
            source_table="raw_src_a.table",
            value_expression="COUNT(*)",
        )

        with (
            patch(f"{_CFG}.get_config", return_value=mock_config),
            patch(f"{_ANALYSIS_CFG}.load_monitors_config", return_value=mock_monitors_config),
            patch(
                f"{_ANALYSIS_TPL}.generate_metrics_for_source",
                return_value=[shared_metric],
            ),
            patch(f"{_ANALYSIS_CFG}.add_monitors_to_config") as mock_add,
        ):
            _ensure_default_metrics(tmp_path, ["src_a", "src_b"])

        # First source adds it, second source should skip it (dedup)
        assert mock_add.call_count == 1
        added = mock_add.call_args[0][1]
        assert len(added) == 1
        assert added[0].name == "shared_metric"
