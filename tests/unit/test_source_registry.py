"""tests/unit/test_source_registry.py

Tests for source registry capabilities metadata (dango/ingestion/sources/registry.py).
"""

from __future__ import annotations

import pytest

from dango.config.models import DataSource, SourceType
from dango.ingestion.sources.registry import (
    SOURCE_REGISTRY,
    get_source_capabilities,
)

CAPABILITY_KEYS = {"performance_metrics", "date_range", "incremental", "custom_queries"}


@pytest.mark.unit
class TestSourceCapabilities:
    """Tests for capability metadata on all registry entries."""

    def test_all_entries_have_capabilities(self) -> None:
        """Every registry entry must include a capabilities dict."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            assert "capabilities" in metadata, f"{source_type} missing capabilities"

    def test_capabilities_have_required_keys(self) -> None:
        """Every capabilities dict must have at least the 4 required boolean keys."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            caps = metadata["capabilities"]
            assert CAPABILITY_KEYS <= set(caps.keys()), (
                f"{source_type} missing required capabilities: {CAPABILITY_KEYS - set(caps.keys())}"
            )

    def test_capabilities_values_are_bool(self) -> None:
        """All capability values must be booleans, except 'incremental' which may be
        None to indicate 'derive at runtime' (e.g., dlt_native sources)."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            for key, value in metadata["capabilities"].items():
                assert isinstance(value, (bool, type(None))), (
                    f"{source_type}.capabilities.{key} is {type(value).__name__}, expected bool or None"
                )
                if key != "incremental":
                    assert isinstance(value, bool), (
                        f"{source_type}.capabilities.{key} is {type(value).__name__}, expected bool"
                    )

    def test_get_source_capabilities_known_source(self) -> None:
        """get_source_capabilities returns correct dict for a known source."""
        caps = get_source_capabilities("google_analytics")
        assert caps is not None
        assert caps["performance_metrics"] is True
        assert caps["date_range"] is True
        assert caps["incremental"] is True
        assert caps["custom_queries"] is True

    def test_get_source_capabilities_unknown_source(self) -> None:
        """get_source_capabilities returns None for an unknown source."""
        assert get_source_capabilities("nonexistent_source") is None

    def test_get_source_capabilities_csv(self) -> None:
        """CSV has incremental but no other capabilities."""
        caps = get_source_capabilities("csv")
        assert caps is not None
        assert caps["performance_metrics"] is False
        assert caps["date_range"] is False
        assert caps["incremental"] is True
        assert caps["custom_queries"] is False

    def test_column_descriptions_stripe_charges(self) -> None:
        """Stripe registry entry has column_descriptions for the charge resource with >=5 entries."""
        stripe = SOURCE_REGISTRY["stripe"]
        assert "column_descriptions" in stripe
        charges = stripe["column_descriptions"].get("charge", {})
        assert len(charges) >= 5, f"Expected >=5 charge descriptions, got {len(charges)}"

    def test_column_descriptions_are_strings(self) -> None:
        """All column description values in all sources must be non-empty strings."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            col_descs = metadata.get("column_descriptions", {})
            for resource_name, cols in col_descs.items():
                for col_name, desc in cols.items():
                    assert isinstance(desc, str) and desc.strip(), (
                        f"{source_type}.column_descriptions.{resource_name}.{col_name} "
                        f"must be a non-empty string"
                    )

    def test_column_descriptions_yaml_safe(self) -> None:
        """Column descriptions must be YAML-safe when quoted in dbt sources.yml."""
        import yaml

        for source_type, metadata in SOURCE_REGISTRY.items():
            col_descs = metadata.get("column_descriptions", {})
            for resource_name, cols in col_descs.items():
                for col_name, desc in cols.items():
                    # Test that description is valid when quoted in YAML
                    test_yaml = f'description: "{desc}"'
                    try:
                        yaml.safe_load(test_yaml)
                    except yaml.YAMLError as e:
                        raise AssertionError(
                            f"{source_type}.column_descriptions.{resource_name}.{col_name} "
                            f"contains YAML-unsafe characters: {desc}\nError: {e}"
                        ) from e


@pytest.mark.unit
class TestSourceTypeRegistryDrift:
    """Regression tests for SourceType/SOURCE_REGISTRY drift (1.0.8-Q3).

    Real bug: SOURCE_REGISTRY["mysql"] had wizard_enabled=True but "mysql" was never
    added to the SourceType enum, so `dango source add` crashed with a Pydantic
    ValidationError on save after a user completed the entire MySQL wizard flow.
    """

    def test_mysql_source_saves_without_error(self) -> None:
        """DataSource(type='mysql') must construct without raising.

        This is the actual regression test for the bug: before the fix, this raised
        a Pydantic ValidationError because 'mysql' was not a SourceType enum member.
        """
        ds = DataSource(name="test", type="mysql")
        assert ds.type == SourceType.MYSQL

    def test_every_wizard_enabled_registry_entry_has_matching_enum_value(self) -> None:
        """Every wizard-enabled registry entry must have a matching SourceType value.

        Catches the exact class of bug that caused the mysql crash: a registry entry
        with wizard_enabled=True that has no corresponding SourceType enum member.
        Without a matching enum value, the wizard walks the user through the full
        flow and then crashes with a ValidationError on save, losing everything they
        entered.
        """
        wizard_enabled_keys = [
            key for key, metadata in SOURCE_REGISTRY.items() if metadata.get("wizard_enabled")
        ]
        assert wizard_enabled_keys, "Expected at least one wizard_enabled registry entry"

        missing = []
        for key in wizard_enabled_keys:
            try:
                SourceType(key)
            except ValueError:
                missing.append(key)

        assert not missing, (
            f"wizard_enabled registry entries with no matching SourceType enum value: "
            f"{missing}. These would crash `dango source add` with a Pydantic "
            f"ValidationError after the user completes the wizard."
        )

    def test_scrapy_no_longer_a_valid_source_type(self) -> None:
        """scrapy was removed from SourceType — it had zero implementation anywhere
        (no registry entry, no dlt_runner.py handling) and was vestigial."""
        with pytest.raises(ValueError):
            SourceType("scrapy")
