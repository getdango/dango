"""tests/unit/test_mcp_registration.py

Every MCP tool module listed in mcp_server._REGISTRATION_MODULES is imported, and
every tool / CLI subcommand it defines is registered.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect

import pytest

from dango.cli.commands import mcp_server


def _tool_names_defined_in(module) -> set[str]:
    """Names of functions decorated with @mcp.tool() in `module` (source scan)."""
    import re

    source = inspect.getsource(module)
    return set(re.findall(r"@mcp\.tool\([^)]*\)\s*(?:async\s+)?def\s+(\w+)", source))


@pytest.mark.unit
def test_all_tool_modules_registered() -> None:
    modules = [importlib.import_module("dango.cli.commands.mcp_server")]
    modules += [
        importlib.import_module(f"dango.cli.commands.{name}")
        for name in mcp_server._REGISTRATION_MODULES
    ]
    expected: set[str] = set()
    for module in modules:
        expected |= _tool_names_defined_in(module)
    assert {"run_sync", "run_transform", "run_doctor", "create_source"} <= expected

    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert expected <= registered


@pytest.mark.unit
def test_mcp_cli_subcommands_registered() -> None:
    assert {"setup", "status", "remove", "run"} <= set(mcp_server.mcp_group.commands)
