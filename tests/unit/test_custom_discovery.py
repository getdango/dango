"""tests/unit/test_custom_discovery.py

Tests for dango.ingestion.sources.custom_discovery — auto-discovery engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.ingestion.sources.custom_discovery import (
    DiscoveredCustomSource,
    _match_dlt_resource_decorator,
    _match_dlt_source_decorator,
    _parse_dlt_source_ast,
    clear_discovery_cache,
    discover_custom_sources,
    get_discovered_source,
)

# ---------------------------------------------------------------------------
# Fixtures: create temporary .py files simulating custom dlt sources
# ---------------------------------------------------------------------------


@pytest.fixture
def merge_source_file(tmp_path: Path) -> Path:
    """A custom source with merge write_disposition and @dlt.source decorator."""
    content = """\"\"\"Daily prices from yfinance.\"\"\"
import dlt

@dlt.source
def investment_desk_prices_daily(
    start_date: str = "2021-01-01",
    lookback_days: int = 5,
):
    \"\"\"Daily prices and FX rates from yfinance.

    Args:
        start_date: How far back to load on first sync (YYYY-MM-DD).
        lookback_days: Days to re-fetch on incremental runs for corrections.
    \"\"\"

    @dlt.resource(
        name="prices_daily",
        write_disposition="merge",
        primary_key=["ticker", "date"],
    )
    def prices_daily():
        yield {}
"""
    filepath = tmp_path / "investment_desk_prices_daily.py"
    filepath.write_text(content)
    return filepath


@pytest.fixture
def replace_source_file(tmp_path: Path) -> Path:
    """A custom source with replace write_disposition."""
    content = '''"""Latest prices from yfinance. Full refresh every run."""
import dlt

@dlt.source
def investment_desk_prices_latest():
    """Latest prices and FX rates from yfinance. Full refresh every run."""

    prices_latest(tickers=[], fetched_at=None)


@dlt.resource(
    write_disposition="replace",
    primary_key="ticker",
)
def prices_latest(tickers, fetched_at):
    """Current/last traded price per ticker."""
    yield {}
'''
    filepath = tmp_path / "investment_desk_prices_latest.py"
    filepath.write_text(content)
    return filepath


@pytest.fixture
def named_source_file(tmp_path: Path) -> Path:
    """A custom source with explicit @dlt.source(name=...) kwarg."""
    content = '''"""Snapshot negative keywords."""
import dlt

@dlt.source(name="badang_labs_negative_keywords", max_table_nesting=0)
def badang_labs_negative_keywords():
    """Snapshot all active negative keywords in the account."""

    @dlt.resource(
        name="negative_keywords",
        write_disposition="merge",
        primary_key=["snapshot_date", "keyword_text"],
    )
    def negative_keywords():
        yield {}
'''
    filepath = tmp_path / "badang_labs_negative_keywords.py"
    filepath.write_text(content)
    return filepath


@pytest.fixture
def no_dlt_source_file(tmp_path: Path) -> Path:
    """A Python file without any @dlt.source decorator."""
    content = '''"""Helper utilities — not a dlt source."""
import dlt

def helper_function():
    """Just a regular function."""
    return 42
'''
    filepath = tmp_path / "helpers.py"
    filepath.write_text(content)
    return filepath


@pytest.fixture
def syntax_error_file(tmp_path: Path) -> Path:
    """A Python file with a syntax error."""
    filepath = tmp_path / "broken.py"
    filepath.write_text("this is not valid python {{{")
    return filepath


# ---------------------------------------------------------------------------
# AST parsing tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestASTParsing:
    def test_detects_merge_write_disposition(self, merge_source_file: Path) -> None:
        result = _parse_dlt_source_ast(merge_source_file)
        assert result["function_name"] == "investment_desk_prices_daily"
        assert result["is_replace_mode"] is False
        assert result["source_name"] is None

    def test_detects_replace_write_disposition(self, replace_source_file: Path) -> None:
        result = _parse_dlt_source_ast(replace_source_file)
        assert result["function_name"] == "investment_desk_prices_latest"
        assert result["is_replace_mode"] is True

    def test_extracts_source_name_from_decorator_kwarg(self, named_source_file: Path) -> None:
        result = _parse_dlt_source_ast(named_source_file)
        assert result["function_name"] == "badang_labs_negative_keywords"
        assert result["source_name"] == "badang_labs_negative_keywords"
        assert result["is_replace_mode"] is False

    def test_skips_file_without_dlt_source(self, no_dlt_source_file: Path) -> None:
        result = _parse_dlt_source_ast(no_dlt_source_file)
        assert result["function_name"] is None

    def test_handles_syntax_error_gracefully(self, syntax_error_file: Path) -> None:
        result = _parse_dlt_source_ast(syntax_error_file)
        assert result == {}


# ---------------------------------------------------------------------------
# Decorator matching tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecoratorMatchers:
    def test_matches_bare_dlt_source(self) -> None:
        import ast

        code = "@dlt.source\ndef foo():\n    pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        decorator = func.decorator_list[0]
        result = _match_dlt_source_decorator(decorator)
        assert result == {}

    def test_matches_dlt_source_with_name_kwarg(self) -> None:
        import ast

        code = '@dlt.source(name="my_source")\ndef foo():\n    pass'
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        decorator = func.decorator_list[0]
        result = _match_dlt_source_decorator(decorator)
        assert result == {"name": "my_source"}

    def test_does_not_match_dlt_resource_as_source(self) -> None:
        import ast

        code = '@dlt.resource(write_disposition="merge")\ndef foo():\n    pass'
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        decorator = func.decorator_list[0]
        result = _match_dlt_source_decorator(decorator)
        assert result is None

    def test_matches_dlt_resource_with_write_disposition(self) -> None:
        import ast

        code = '@dlt.resource(write_disposition="replace")\ndef foo():\n    pass'
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        decorator = func.decorator_list[0]
        result = _match_dlt_resource_decorator(decorator)
        assert result == {"write_disposition": "replace"}

    def test_matches_bare_dlt_resource(self) -> None:
        import ast

        code = "@dlt.resource\ndef foo():\n    pass"
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        decorator = func.decorator_list[0]
        result = _match_dlt_resource_decorator(decorator)
        assert result == {}


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoverCustomSources:
    def setup_method(self) -> None:
        clear_discovery_cache()

    def test_discovers_all_sources_in_directory(
        self,
        merge_source_file: Path,
        replace_source_file: Path,
        named_source_file: Path,
    ) -> None:
        custom_sources_dir = merge_source_file.parent
        result = discover_custom_sources(custom_sources_dir)

        # named_source_file has explicit source_name
        assert "badang_labs_negative_keywords" in result
        assert "investment_desk_prices_daily" in result
        assert "investment_desk_prices_latest" in result

        # Verify merge source
        merge_src = result["investment_desk_prices_daily"]
        assert merge_src.module_name == "investment_desk_prices_daily"
        assert merge_src.is_replace_mode is False

        # Verify replace source
        replace_src = result["investment_desk_prices_latest"]
        assert replace_src.is_replace_mode is True

    def test_skips_non_dlt_files(self, merge_source_file: Path, no_dlt_source_file: Path) -> None:
        result = discover_custom_sources(merge_source_file.parent)
        # Only @dlt.source files are returned
        assert "helpers" not in result
        assert "investment_desk_prices_daily" in result

    def test_skips_syntax_error_files(self, syntax_error_file: Path) -> None:
        result = discover_custom_sources(syntax_error_file.parent)
        assert "broken" not in result

    def test_empty_for_nonexistent_directory(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nonexistent"
        result = discover_custom_sources(nonexistent)
        assert result == {}

    def test_get_discovered_source_returns_single(self, merge_source_file: Path) -> None:
        custom_sources_dir = merge_source_file.parent
        result = get_discovered_source(custom_sources_dir, "investment_desk_prices_daily")
        assert result is not None
        assert result.module_name == "investment_desk_prices_daily"

    def test_get_discovered_source_returns_none_for_unknown(self, merge_source_file: Path) -> None:
        custom_sources_dir = merge_source_file.parent
        result = get_discovered_source(custom_sources_dir, "nonexistent")
        assert result is None

    def test_cache_mtime_invalidation(self, merge_source_file: Path) -> None:
        custom_sources_dir = merge_source_file.parent
        cache: dict[str, tuple[float, DiscoveredCustomSource]] = {}

        # First call: populate cache
        result1 = discover_custom_sources(custom_sources_dir, cache=cache)
        assert len(cache) >= 1

        # Second call with same mtime: uses cache
        result2 = discover_custom_sources(custom_sources_dir, cache=cache)
        assert result1.keys() == result2.keys()

        # Third call with empty cache: re-discovers
        result3 = discover_custom_sources(custom_sources_dir, cache={})
        assert result1.keys() == result3.keys()

    def test_skips_underscore_prefixed_files(self, tmp_path: Path) -> None:
        """Files starting with _ (like __init__.py) should be skipped."""
        content = '''import dlt

@dlt.source
def hidden_source():
    """Should not be discovered."""
    pass
'''
        (tmp_path / "_hidden.py").write_text(content)

        result = discover_custom_sources(tmp_path)
        assert "hidden_source" not in result


# ---------------------------------------------------------------------------
# DiscoveredCustomSource dataclass tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoveredCustomSource:
    def test_defaults(self) -> None:
        dcs = DiscoveredCustomSource(
            module_name="test_module",
            function_name="test_func",
        )
        assert dcs.module_name == "test_module"
        assert dcs.function_name == "test_func"
        assert dcs.source_name is None
        assert dcs.docstring is None
        assert dcs.parameters == {}
        assert dcs.is_replace_mode is False
        assert dcs.file_mtime == 0.0

    def test_canonical_name_prefers_source_name(self) -> None:
        dcs = DiscoveredCustomSource(
            module_name="test_module",
            function_name="test_func",
            source_name="explicit_name",
        )
        # The source_name should be used as canonical name
        assert dcs.source_name == "explicit_name"
