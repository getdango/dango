"""dango/ingestion/sources/custom_discovery.py

Auto-discovery engine for custom dlt sources in custom_sources/ directory.

Scans Python files for @dlt.source decorated functions and extracts metadata
without requiring manual sources.yml configuration.

Two-phase extraction:
1. AST parsing (safe, no code execution): write_disposition from @dlt.resource()
2. importlib (executes user code, same as dlt_runner.py): function signature + docstring
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dango.logging import get_logger

logger = get_logger(__name__)

# Module-level discovery cache: {file_path_str: (mtime, DiscoveredCustomSource)}
_discovery_cache: dict[str, tuple[float, DiscoveredCustomSource]] = {}


@dataclass
class DiscoveredCustomSource:
    """Metadata extracted from a single custom dlt source Python file."""

    module_name: str  # e.g., "investment_desk_prices_daily"
    function_name: str  # e.g., "investment_desk_prices_daily"
    source_name: str | None = None  # from @dlt.source(name=...) kwarg
    docstring: str | None = None  # function __doc__
    parameters: dict[str, Any] = field(default_factory=dict)  # {param: default}
    is_replace_mode: bool = False  # True if ANY resource uses replace write_disposition
    file_mtime: float = 0.0  # for cache invalidation


def _parse_dlt_source_ast(filepath: Path) -> dict[str, Any]:
    """AST-based extraction of @dlt.source and @dlt.resource decorator metadata.

    Does NOT execute the Python file. Safe for untrusted code.

    Returns dict with keys:
        function_name: str | None
        source_name: str | None  (from @dlt.source(name=...) kwarg)
        is_replace_mode: bool
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("Syntax error parsing %s, skipping", filepath.name)
        return {}

    result: dict[str, Any] = {
        "function_name": None,
        "source_name": None,
        "is_replace_mode": False,
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        for decorator in node.decorator_list:
            # Match @dlt.source or @dlt.source(...)
            source_info = _match_dlt_source_decorator(decorator)
            if source_info is not None:
                result["function_name"] = node.name
                if source_info.get("name"):
                    result["source_name"] = source_info["name"]
                continue

            # Match @dlt.resource(...) or @dlt.resource
            resource_info = _match_dlt_resource_decorator(decorator)
            if resource_info is not None and resource_info.get("write_disposition") == "replace":
                result["is_replace_mode"] = True

    return result


def _match_dlt_source_decorator(decorator: ast.expr) -> dict[str, Any] | None:
    """Check if decorator matches @dlt.source or @dlt.source(...).

    Returns dict with 'name' kwarg if present, or empty dict for bare @dlt.source.
    Returns None if decorator does not match.
    """
    # Bare @dlt.source (no parentheses)
    if isinstance(decorator, ast.Attribute):
        if (
            isinstance(decorator.value, ast.Name)
            and decorator.value.id == "dlt"
            and decorator.attr == "source"
        ):
            return {}

    # @dlt.source(...) with arguments
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "dlt"
            and func.attr == "source"
        ):
            info: dict[str, Any] = {}
            for kw in decorator.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    info["name"] = kw.value.value
            return info

    return None


def _match_dlt_resource_decorator(decorator: ast.expr) -> dict[str, Any] | None:
    """Check if decorator matches @dlt.resource or @dlt.resource(...).

    Returns dict with keyword argument values (write_disposition, name, primary_key).
    Returns None if decorator does not match.
    """
    # Bare @dlt.resource (no parentheses) — no kwargs to extract
    if isinstance(decorator, ast.Attribute):
        if (
            isinstance(decorator.value, ast.Name)
            and decorator.value.id == "dlt"
            and decorator.attr == "resource"
        ):
            return {}

    # @dlt.resource(...) with arguments
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "dlt"
            and func.attr == "resource"
        ):
            info: dict[str, Any] = {}
            for kw in decorator.keywords:
                if kw.arg in ("write_disposition", "name") and isinstance(kw.value, ast.Constant):
                    info[kw.arg] = kw.value.value
                elif kw.arg == "primary_key":
                    info["has_primary_key"] = True
            return info

    return None


def _import_and_inspect(
    filepath: Path, module_name: str, target_function_name: str
) -> dict[str, Any]:
    """Import the module and extract function signature + docstring.

    Executes the Python file (same as dlt_runner.py does at sync time).
    Falls back gracefully on import errors.

    Args:
        filepath: Path to the Python file.
        module_name: Module name for import.
        target_function_name: Name of the @dlt.source function (from AST phase).
            Used to find the exact function rather than guessing.

    Returns dict with keys: parameters, docstring
    """
    result: dict[str, Any] = {
        "parameters": {},
        "docstring": None,
    }

    try:
        # Ensure the custom_sources parent directory is on sys.path
        custom_sources_dir = str(filepath.parent)
        if custom_sources_dir not in sys.path:
            sys.path.insert(0, custom_sources_dir)

        try:
            spec = importlib.util.spec_from_file_location(module_name, str(filepath))
            if spec is None or spec.loader is None:
                logger.warning("Could not create module spec for %s", module_name)
                return result
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
        except Exception:
            logger.warning(
                "Could not import %s for metadata extraction", module_name, exc_info=True
            )
            return result

        # Look up the exact function identified by AST parsing
        source_func = getattr(module, target_function_name, None)
        if source_func is None or not callable(source_func):
            logger.debug(
                "Function %s not found in module %s after import",
                target_function_name,
                module_name,
            )
            return result

        # Extract docstring
        result["docstring"] = inspect.getdoc(source_func)

        # Extract parameter defaults
        try:
            sig = inspect.signature(source_func)
            for param_name, param in sig.parameters.items():
                if param.default is not inspect.Parameter.empty:
                    result["parameters"][param_name] = param.default
        except (ValueError, TypeError):
            logger.debug(
                "Could not inspect signature for %s.%s",
                module_name,
                target_function_name,
            )

    finally:
        # Clean up sys.path
        if custom_sources_dir in sys.path:
            sys.path.remove(custom_sources_dir)

    return result


def discover_custom_sources(
    custom_sources_dir: Path,
    cache: dict[str, tuple[float, DiscoveredCustomSource]] | None = None,
) -> dict[str, DiscoveredCustomSource]:
    """Scan custom_sources/ directory for @dlt.source decorated Python files.

    Uses two-phase extraction:
    1. AST parsing for write_disposition (safe, no code execution)
    2. importlib for function signature + docstring (same as dlt_runner.py)

    Args:
        custom_sources_dir: Path to the custom_sources/ directory.
        cache: Optional mutable cache dict keyed by file path string.
               If provided, entries with unchanged mtime are reused.

    Returns:
        Dict mapping canonical source name -> DiscoveredCustomSource.
    """
    if cache is None:
        cache = _discovery_cache

    if not custom_sources_dir.exists() or not custom_sources_dir.is_dir():
        return {}

    discovered: dict[str, DiscoveredCustomSource] = {}

    for filepath in sorted(custom_sources_dir.glob("*.py")):
        if filepath.name.startswith("_"):
            continue

        mtime = filepath.stat().st_mtime
        cache_key = str(filepath)

        # Check cache
        if cache_key in cache:
            cached_mtime, cached_source = cache[cache_key]
            if cached_mtime == mtime:
                discovered[cached_source.source_name or cached_source.function_name] = cached_source
                continue

        module_name = filepath.stem

        # Phase 1: AST parsing (safe, no code execution)
        ast_data = _parse_dlt_source_ast(filepath)
        function_name = ast_data.get("function_name") or module_name
        source_name = ast_data.get("source_name")
        is_replace_mode = ast_data.get("is_replace_mode", False)

        if ast_data.get("function_name") is None:
            # No @dlt.source decorator found; skip this file
            logger.debug("No @dlt.source decorator in %s, skipping", filepath.name)
            continue

        # Phase 2: Import + inspect for signature and docstring
        import_data = _import_and_inspect(filepath, module_name, function_name)

        canonical_name = source_name or function_name

        dcs = DiscoveredCustomSource(
            module_name=module_name,
            function_name=function_name,
            source_name=source_name,
            docstring=import_data.get("docstring"),
            parameters=import_data.get("parameters", {}),
            is_replace_mode=is_replace_mode,
            file_mtime=mtime,
        )

        # Update cache
        cache[cache_key] = (mtime, dcs)
        discovered[canonical_name] = dcs

    return discovered


def get_discovered_source(
    custom_sources_dir: Path,
    source_name: str,
    discovered: dict[str, DiscoveredCustomSource] | None = None,
) -> DiscoveredCustomSource | None:
    """Look up a single discovered custom source by name.

    Args:
        custom_sources_dir: Path to the custom_sources/ directory.
        source_name: Canonical source name to look up.
        discovered: Optional pre-computed discovery result. If provided,
            avoids re-scanning the directory. Useful when calling in a loop.

    Returns:
        DiscoveredCustomSource if found, None otherwise.
    """
    if discovered is not None:
        return discovered.get(source_name)
    all_discovered = discover_custom_sources(custom_sources_dir)
    return all_discovered.get(source_name)


def clear_discovery_cache() -> None:
    """Clear the module-level discovery cache (useful for testing)."""
    _discovery_cache.clear()
