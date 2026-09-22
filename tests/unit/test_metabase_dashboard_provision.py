"""tests/unit/test_metabase_dashboard_provision.py

Regression tests for `dango dashboard provision`'s known-database-id bypass.
Split out of test_metabase_api.py to stay under the 500-line file-size check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestProvisionPipelineHealthDashboardDatabaseId:
    """Regression: get_database_id()'s default "DuckDB" substring search
    never matches setup_metabase()'s f"{org_name} Analytics" naming — a
    known database_id now bypasses that search (found 2026-09-04)."""

    def test_known_database_id_skips_search(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.authenticate = MagicMock(return_value=True)  # type: ignore[method-assign]
        provisioner.get_database_id = MagicMock()  # type: ignore[method-assign]

        provisioner.provision_pipeline_health_dashboard(database_id=99)

        provisioner.get_database_id.assert_not_called()

    def test_no_database_id_falls_back_to_search(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.authenticate = MagicMock(return_value=True)  # type: ignore[method-assign]
        provisioner.get_database_id = MagicMock(return_value=None)  # type: ignore[method-assign]

        result = provisioner.provision_pipeline_health_dashboard()

        provisioner.get_database_id.assert_called_once()
        assert result["errors"] == ["DuckDB database not found in Metabase"]
