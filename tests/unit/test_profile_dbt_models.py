"""tests/unit/test_profile_dbt_models.py

Unit tests for _profile_dbt_models — downstream model profiling in
intermediate/marts schemas via dbt run_results + manifest discovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.utils.post_sync import _profile_dbt_models


def _write_dbt_artifacts(tmp_path: Path, run_results: dict, manifest: dict) -> None:
    """Write mock dbt target artifacts to tmp_path/dbt/target/."""
    target = tmp_path / "dbt" / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "run_results.json").write_text(json.dumps(run_results))
    (target / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.unit
class TestProfileDbtModels:
    """Tests for _profile_dbt_models — downstream model profiling."""

    @patch("dango.utils.post_sync.profile_table")
    def test_profiles_intermediate_and_marts_tables(self, mock_profile, tmp_path):
        """Successful models in intermediate/marts schemas get profiled."""
        _write_dbt_artifacts(
            tmp_path,
            run_results={
                "results": [
                    {"unique_id": "model.dango.int_orders", "status": "success"},
                    {"unique_id": "model.dango.fct_revenue", "status": "success"},
                ]
            },
            manifest={
                "nodes": {
                    "model.dango.int_orders": {
                        "schema": "intermediate",
                        "alias": "int_orders",
                        "name": "int_orders",
                    },
                    "model.dango.fct_revenue": {
                        "schema": "marts",
                        "alias": "fct_revenue",
                        "name": "fct_revenue",
                    },
                }
            },
        )
        mock_profile.return_value = {}
        _profile_dbt_models(tmp_path)

        assert mock_profile.call_count == 2
        mock_profile.assert_any_call(
            tmp_path, "intermediate", "int_orders", schema_override="intermediate"
        )
        mock_profile.assert_any_call(tmp_path, "marts", "fct_revenue", schema_override="marts")

    @patch("dango.utils.post_sync.profile_table")
    def test_skips_raw_and_staging_models(self, mock_profile, tmp_path):
        """Models in raw_*/staging schemas are skipped (already profiled)."""
        _write_dbt_artifacts(
            tmp_path,
            run_results={
                "results": [
                    {"unique_id": "model.dango.stg_orders", "status": "success"},
                    {"unique_id": "model.dango.raw_stuff", "status": "success"},
                    {"unique_id": "model.dango.fct_sales", "status": "success"},
                ]
            },
            manifest={
                "nodes": {
                    "model.dango.stg_orders": {
                        "schema": "staging",
                        "alias": "stg_orders",
                        "name": "stg_orders",
                    },
                    "model.dango.raw_stuff": {
                        "schema": "raw_shopify",
                        "alias": "raw_stuff",
                        "name": "raw_stuff",
                    },
                    "model.dango.fct_sales": {
                        "schema": "marts",
                        "alias": "fct_sales",
                        "name": "fct_sales",
                    },
                }
            },
        )
        mock_profile.return_value = {}
        _profile_dbt_models(tmp_path)

        assert mock_profile.call_count == 1
        mock_profile.assert_called_once_with(
            tmp_path, "marts", "fct_sales", schema_override="marts"
        )

    @patch("dango.utils.post_sync.profile_table")
    def test_no_run_results_graceful(self, mock_profile, tmp_path):
        """Missing run_results.json → returns without error."""
        _profile_dbt_models(tmp_path)
        mock_profile.assert_not_called()

    @patch("dango.utils.post_sync.profile_table")
    def test_no_manifest_graceful(self, mock_profile, tmp_path):
        """run_results.json exists but no manifest.json → returns without error."""
        target = tmp_path / "dbt" / "target"
        target.mkdir(parents=True)
        (target / "run_results.json").write_text('{"results": []}')
        _profile_dbt_models(tmp_path)
        mock_profile.assert_not_called()

    @patch("dango.utils.post_sync.profile_table")
    def test_failed_models_skipped(self, mock_profile, tmp_path):
        """Models with status != 'success' are not profiled."""
        _write_dbt_artifacts(
            tmp_path,
            run_results={
                "results": [
                    {"unique_id": "model.dango.fct_ok", "status": "success"},
                    {"unique_id": "model.dango.fct_bad", "status": "error"},
                ]
            },
            manifest={
                "nodes": {
                    "model.dango.fct_ok": {
                        "schema": "marts",
                        "alias": "fct_ok",
                        "name": "fct_ok",
                    },
                    "model.dango.fct_bad": {
                        "schema": "marts",
                        "alias": "fct_bad",
                        "name": "fct_bad",
                    },
                }
            },
        )
        mock_profile.return_value = {}
        _profile_dbt_models(tmp_path)

        assert mock_profile.call_count == 1
        mock_profile.assert_called_once_with(tmp_path, "marts", "fct_ok", schema_override="marts")
