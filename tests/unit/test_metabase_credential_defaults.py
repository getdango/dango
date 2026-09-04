"""tests/unit/test_metabase_credential_defaults.py

Regression tests for 1.0.8-BUGS-FOUND.md's "Hardcoded-looking Metabase credential
defaults on an unused exported function" entry: MetabaseProvisioner() and
provision_dashboard() defaulted username/password to "admin@example.com"/"admin123",
and the only real caller of that default, create_pipeline_health_dashboard(), was
dead code (zero callers, confirmed via grep) that has since been deleted.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestMetabaseCredentialDefaults:
    def test_provisioner_no_longer_defaults_to_guessable_credentials(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        assert provisioner.username != "admin@example.com"
        assert provisioner.password != "admin123"

    def test_provision_dashboard_requires_credentials(self) -> None:
        from dango.visualization.metabase import provision_dashboard

        with pytest.raises(TypeError):
            provision_dashboard()  # type: ignore[call-arg]

    def test_dead_code_function_removed(self) -> None:
        import dango.visualization as viz

        assert not hasattr(viz, "create_pipeline_health_dashboard")
