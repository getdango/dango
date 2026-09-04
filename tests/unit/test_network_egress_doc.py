"""tests/unit/test_network_egress_doc.py

Doc-consistency and real-introspection checks for docs/network-egress.yml —
no network, fast. Split out of test_egress_allowlist.py (which covers live
DNS-blocked egress detection) to stay under the 500-line file-size check.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EGRESS_DOC = REPO_ROOT / "docs" / "network-egress.yml"


def _load_egress_doc() -> dict[str, Any]:
    return yaml.safe_load(EGRESS_DOC.read_text())


class TestNetworkEgressDoc:
    """Doc-consistency and real-introspection checks — no network, fast."""

    def test_network_egress_yml_exists(self) -> None:
        assert EGRESS_DOC.exists(), "docs/network-egress.yml not found"
        data = _load_egress_doc()
        assert "telemetry" in data
        assert "functional" in data

    def test_network_egress_yml_has_all_known_providers(self) -> None:
        data = _load_egress_doc()
        providers: set[str] = set()
        for i, entry in enumerate(data["telemetry"]):
            provider = entry.get("provider")
            assert provider, (
                f"docs/network-egress.yml: telemetry entry {i} is missing a "
                f"'provider' key: {entry!r}"
            )
            providers.add(provider)
        assert providers == {"dango", "dbt-core", "dlt", "metabase"}

    def test_dlt_runtime_configuration_has_telemetry_field(self) -> None:
        """Real introspection against the installed dlt version, not a
        hardcoded string comparison. Catches a silent rename of the field
        docs/network-egress.yml claims controls dlt's telemetry (this is
        exactly the risk LAUNCH-READINESS.md §A2 calls out: "If dlt renames
        dlthub_telemetry... a silent failure means you are telling users
        something false")."""
        from dlt.common.configuration.specs.runtime_configuration import (
            RuntimeConfiguration,
        )

        fields = RuntimeConfiguration.__dataclass_fields__
        assert "dlthub_telemetry" in fields, (
            "dlt renamed its telemetry opt-out field — update "
            "docs/network-egress.yml and the write-through in the telemetry command"
        )

    def test_dbt_send_anonymous_usage_stats_envvar_name(self) -> None:
        """Real check against the installed dbt-core source, not memory.
        Catches a silent rename of the env var docs/network-egress.yml
        documents as dbt's telemetry opt-out control."""
        from dbt.cli import params as dbt_params

        source = inspect.getsource(dbt_params)
        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" in source, (
            "dbt-core's telemetry opt-out env var name changed — update "
            "docs/network-egress.yml and the write-through in the telemetry command"
        )
