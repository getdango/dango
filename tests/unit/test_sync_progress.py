"""tests/unit/test_sync_progress.py

Tests for progress_callback parameter in run_sync().
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_PATCH_TRANSFORM = "dango.transformation"
_PATCH_TRANSFORM_GEN = "dango.transformation.generator"
_PATCH_VIZ = "dango.visualization.metabase"
_PATCH_GOVERNANCE = "dango.governance.schema_drift"
_PATCH_POST_SYNC = "dango.utils.post_sync"


def _make_source(name: str = "test_src", enabled: bool = True) -> MagicMock:
    src = MagicMock()
    src.name = name
    src.enabled = enabled
    return src


@pytest.mark.unit
class TestProgressCallback:
    """Tests for progress_callback in run_sync()."""

    @patch(f"{_PATCH_POST_SYNC}.dispatch_post_sync_hooks")
    @patch(f"{_PATCH_VIZ}.sync_metabase_schema", return_value=True)
    @patch(f"{_PATCH_VIZ}.refresh_metabase_connection", return_value=(True, None))
    @patch(f"{_PATCH_TRANSFORM}.generate_dbt_docs", return_value=(True, ""))
    @patch(f"{_PATCH_TRANSFORM}.run_dbt_models", return_value=(True, ""))
    @patch(f"{_PATCH_GOVERNANCE}.detect_drift_for_sources", return_value=[])
    @patch(f"{_PATCH_TRANSFORM_GEN}.DbtModelGenerator")
    def test_callback_called_at_dbt_phases(
        self,
        mock_gen_cls,
        mock_drift,
        mock_dbt,
        mock_docs,
        mock_refresh,
        mock_schema,
        mock_post_sync,
        tmp_path,
    ):
        """Callback should be called with data_load_complete, dbt_started, dbt_complete."""
        from dango.ingestion.dlt_runner import run_sync

        mock_gen_cls.return_value.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
        }

        callback = MagicMock()
        src = _make_source()

        with patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls:
            mock_runner_cls.return_value.run_source.return_value = {
                "status": "success",
                "rows_loaded": 10,
            }
            mock_runner_cls.return_value.allow_schema_changes = False
            run_sync(
                project_root=tmp_path,
                sources=[src],
                progress_callback=callback,
            )

        phases_called = [call.args[0] for call in callback.call_args_list]
        assert "data_load_complete" in phases_called
        assert "dbt_started" in phases_called
        assert "dbt_complete" in phases_called

    @patch(f"{_PATCH_TRANSFORM_GEN}.DbtModelGenerator")
    def test_callback_not_called_when_skip_dbt(
        self,
        mock_gen_cls,
        tmp_path,
    ):
        """With skip_dbt=True, only data_load_complete fires (no dbt phases)."""
        from dango.ingestion.dlt_runner import run_sync

        mock_gen_cls.return_value.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
        }

        callback = MagicMock()
        src = _make_source()

        with patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls:
            mock_runner_cls.return_value.run_source.return_value = {
                "status": "success",
                "rows_loaded": 5,
            }
            mock_runner_cls.return_value.allow_schema_changes = False
            run_sync(
                project_root=tmp_path,
                sources=[src],
                skip_dbt=True,
                progress_callback=callback,
            )

        phases_called = [call.args[0] for call in callback.call_args_list]
        assert "data_load_complete" in phases_called
        assert "dbt_started" not in phases_called
        assert "dbt_complete" not in phases_called

    def test_no_callback_no_error(self, tmp_path):
        """progress_callback=None (default) should not raise."""
        from dango.ingestion.dlt_runner import run_sync

        src = _make_source()

        with patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls:
            mock_runner_cls.return_value.run_source.return_value = {
                "status": "success",
                "rows_loaded": 0,
            }
            mock_runner_cls.return_value.allow_schema_changes = False
            result = run_sync(
                project_root=tmp_path,
                sources=[src],
                skip_dbt=True,
            )

        assert result["success_count"] == 1

    @patch(f"{_PATCH_POST_SYNC}.dispatch_post_sync_hooks")
    @patch(f"{_PATCH_TRANSFORM}.run_dbt_models", return_value=(False, "compilation error"))
    @patch(f"{_PATCH_GOVERNANCE}.detect_drift_for_sources", return_value=[])
    @patch(f"{_PATCH_TRANSFORM_GEN}.DbtModelGenerator")
    def test_callback_dbt_failed_on_dbt_failure(
        self,
        mock_gen_cls,
        mock_drift,
        mock_dbt,
        mock_post_sync,
        tmp_path,
    ):
        """When dbt fails, callback should fire dbt_failed instead of dbt_complete."""
        from dango.ingestion.dlt_runner import run_sync

        mock_gen_cls.return_value.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
        }

        callback = MagicMock()
        src = _make_source()

        with patch("dango.ingestion.dlt_runner.DltPipelineRunner") as mock_runner_cls:
            mock_runner_cls.return_value.run_source.return_value = {
                "status": "success",
                "rows_loaded": 10,
            }
            mock_runner_cls.return_value.allow_schema_changes = False
            run_sync(
                project_root=tmp_path,
                sources=[src],
                progress_callback=callback,
            )

        phases_called = [call.args[0] for call in callback.call_args_list]
        assert "data_load_complete" in phases_called
        assert "dbt_started" in phases_called
        assert "dbt_failed" in phases_called
        assert "dbt_complete" not in phases_called

    def test_no_callback_when_no_success_sources(self, tmp_path):
        """Callback should not be called when all sources are disabled."""
        from dango.ingestion.dlt_runner import run_sync

        callback = MagicMock()
        src = _make_source(enabled=False)

        run_sync(
            project_root=tmp_path,
            sources=[src],
            progress_callback=callback,
        )

        callback.assert_not_called()
