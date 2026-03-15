"""tests/unit/test_registry_params.py

Regression test verifying that all registry optional_params match the actual
function signatures of the corresponding dlt source functions. Catches mismatches
like P5-012 (Facebook Ads start_date) and P5-016 (Salesforce, Slack, Notion, etc.)
before they become runtime TypeErrors.
"""

import importlib
import inspect

import pytest

from dango.ingestion.sources.registry import SOURCE_REGISTRY

# Params intercepted by dlt_runner before reaching source functions.
# These are valid in optional_params even if the function doesn't accept them.
INTERCEPTED_PARAMS = {"resources"}

# Sources that use dlt built-in packages (not vendored in dango.ingestion.dlt_sources)
# These can't be introspected without installing extra dependencies.
BUILTIN_DLT_PACKAGES = {"filesystem", "rest_api", "sql_database"}


def _get_importable_sources() -> list[tuple[str, dict]]:
    """Return registry entries with vendored dlt source functions."""
    sources = []
    for source_key, metadata in SOURCE_REGISTRY.items():
        dlt_package = metadata.get("dlt_package")
        dlt_function = metadata.get("dlt_function")
        if not dlt_package or not dlt_function:
            continue
        if dlt_package in BUILTIN_DLT_PACKAGES:
            continue
        sources.append((source_key, metadata))
    return sources


_IMPORTABLE_SOURCES = _get_importable_sources()


@pytest.mark.unit
class TestRegistryParamsMatchSignatures:
    """Verify optional_params names match actual dlt source function signatures."""

    @pytest.mark.parametrize(
        "source_key,metadata",
        _IMPORTABLE_SOURCES,
        ids=[s[0] for s in _IMPORTABLE_SOURCES],
    )
    def test_optional_params_accepted_by_source_function(
        self, source_key: str, metadata: dict
    ) -> None:
        """Each optional_param must be in the function signature or INTERCEPTED_PARAMS."""
        dlt_package = metadata["dlt_package"]
        dlt_function = metadata["dlt_function"]

        module_path = f"dango.ingestion.dlt_sources.{dlt_package}"
        try:
            source_module = importlib.import_module(module_path)
        except ImportError:
            pytest.skip(f"Cannot import {module_path}")
            return

        func = getattr(source_module, dlt_function, None)
        assert func is not None, f"{dlt_function} not found in {module_path}"

        sig = inspect.signature(func)
        accepted_params = set(sig.parameters.keys())

        optional_params = metadata.get("optional_params", [])
        for param in optional_params:
            param_name = param["name"]
            assert param_name in accepted_params or param_name in INTERCEPTED_PARAMS, (
                f"Source '{source_key}': optional_param '{param_name}' is not accepted "
                f"by {dlt_function}() and not in INTERCEPTED_PARAMS. "
                f"Function accepts: {sorted(accepted_params)}"
            )
