"""dango/cli/commands/mcp_mutations.py

MCP mutation tools for Dango: the tools that let an LLM create sources (add_source,
list_source_types), as opposed to the read-only tools in mcp_server.py.
Operate tools (sync/transform/doctor) live in mcp_operations.py.
Schedule tools live in mcp_schedules.py; model tools live in mcp_models.py.

Split out of mcp_server.py (Session E's read tools + FastMCP server
definition already sit at 454 lines) to keep both files under the project's
500-line file-size check, mirroring the existing mcp_setup.py /
mcp_helpers.py split documented in mcp_server.py's own module docstring and
dango/cli/CLAUDE.md. Registers onto the shared `mcp` FastMCP instance via a
bottom-of-file import in mcp_server.py (same pattern mcp_setup.py uses for
`mcp_group`).

CRITICAL: all *dango.* imports that touch real project/database/config state
are lazy (inside function bodies), never at module top level — same rule as
mcp_server.py. The exceptions are `from dango.cli.commands.mcp_server import
mcp` (a reference to the already-constructed FastMCP instance) and `from
dango.cli.commands.mcp_helpers import _get_project_root` below: mirrors
mcp_server.py's own top-level import of the same helper (mcp_helpers.py does
zero dango.* imports at its own module level — see its docstring), and keeps
`mcp_mutations._get_project_root` monkeypatchable in tests the same way
`mcp_server._get_project_root` already is.
"""

from __future__ import annotations

from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root, _git_warnings
from dango.cli.commands.mcp_server import mcp

# ── Create tools ──────────────────────────────────────────────────────────────


@mcp.tool()
def add_source(
    source_type: str,
    source_name: str,
    description: str = "",
) -> dict[str, Any]:
    """Add a new data source to sources.yml.

    Creates the configuration entry only. Credentials must be set up separately
    using `dango oauth <source>` or `dango source edit <name>`.

    Args:
        source_type: Source type from the registry (e.g. "google_sheets", "stripe",
                     "google_ads", "facebook_ads", "postgres"). Run list_source_types()
                     to see all available types.
        source_name: Unique name for this source instance (e.g. "my_stripe_prod").
                     Use lowercase_with_underscores. Must not already exist.
        description: Human-readable description of what this source contains.

    Returns dict with: status, source_name, next_steps (credentials to configure), git_warning.
    """
    project_root = _get_project_root()
    from dango.config.helpers import load_config, save_config
    from dango.config.models import DataSource, SourceType
    from dango.ingestion.sources.registry import get_source_metadata

    # Validate source type
    try:
        stype = SourceType(source_type)
    except ValueError:
        return {
            "error": f"Unknown source type: '{source_type}'. Run list_source_types() to see options."
        }

    # Check name uniqueness
    config = load_config(project_root)
    if config and config.sources.get_source(source_name):
        return {"error": f"Source '{source_name}' already exists in sources.yml"}

    # Get metadata for next steps
    meta = get_source_metadata(source_type) or {}
    auth_type = meta.get("auth_type", "none")

    # Create source entry
    new_source = DataSource(
        name=source_name,
        type=stype,
        enabled=True,
        description=description or f"{source_type} data source",
    )

    if config and config.sources:
        config.sources.sources.append(new_source)
        # Correction (coordinating-chat pre-dispatch verification, 2026-09-03):
        # save_config()'s real signature (dango/config/helpers.py) is
        # save_config(config, project_root=None) — config first. The
        # original snippet here had the arguments reversed, which would
        # either raise or silently corrupt state depending on how
        # save_config duck-types its first argument.
        save_config(config, project_root)
    else:
        return {"error": "Could not load project configuration"}

    next_steps = []
    if auth_type == "oauth":
        next_steps.append(f"Run: dango oauth {source_type}")
    elif auth_type == "api_key":
        key_name = meta.get("secret_key", f"{source_type.upper()}_API_KEY")
        next_steps.append(f"Add {key_name} to .dlt/secrets.toml")
    next_steps.append(f"Run: dango sync {source_name}")

    result = {
        "status": "created",
        "source_name": source_name,
        "source_type": source_type,
        "auth_type": auth_type,
        "next_steps": next_steps,
    }
    if git_warning := _git_warnings(project_root):
        result["git_warning"] = git_warning
    return result


@mcp.tool()
def list_source_types() -> list[dict[str, Any]]:
    """List all available source types in the Dango registry.

    Returns list of dicts with: type, name, description, auth_type, category.
    """
    from dango.ingestion.sources.registry import SOURCE_REGISTRY

    return [
        {
            "type": k,
            "name": v.get("display_name", k),
            "description": v.get("description", ""),
            "auth_type": v.get("auth_type", "none"),
            "category": v.get("category", "other"),
        }
        for k, v in SOURCE_REGISTRY.items()
        if v.get("wizard_enabled", True)
    ]
