"""tests/unit/test_source_registry.py

Tests for source registry capabilities metadata (dango/ingestion/sources/registry.py).
"""

from __future__ import annotations

import pytest

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
        """Every capabilities dict must have exactly the 4 required boolean keys."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            caps = metadata["capabilities"]
            assert set(caps.keys()) == CAPABILITY_KEYS, (
                f"{source_type} capabilities keys mismatch: {set(caps.keys())}"
            )

    def test_capabilities_values_are_bool(self) -> None:
        """All capability values must be booleans."""
        for source_type, metadata in SOURCE_REGISTRY.items():
            for key, value in metadata["capabilities"].items():
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
