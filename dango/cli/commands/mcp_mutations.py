"""dango/cli/commands/mcp_mutations.py

MCP mutation tools for Dango: the tools that let an LLM actually operate
Dango (sync data, run transforms, create sources, create models, add
schedules), as opposed to the read-only tools in mcp_server.py.

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

from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root
from dango.cli.commands.mcp_server import mcp

# ── Trigger tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def run_sync(source_name: str, full_refresh: bool = False) -> dict[str, Any]:
    """Sync a data source. Respects the existing lock and queue semantics.

    Args:
        source_name: Name of the source to sync (as defined in sources.yml)
        full_refresh: If True, drop and reload all data (default False)

    Returns dict with: status, rows_loaded, duration_seconds, error (if failed).
    """
    project_root = _get_project_root()
    from dango.config.helpers import load_config
    from dango.ingestion import run_sync as _run_sync

    config = load_config(project_root)
    if not config:
        return {"error": "No project configuration found"}

    source = config.sources.get_source(source_name)
    if source is None:
        return {"error": f"Source '{source_name}' not found in sources.yml"}

    try:
        # Correction (coordinating-chat pre-dispatch verification, 2026-09-03):
        # run_sync()'s real signature (dango/ingestion/dlt_runner.py) is
        # run_sync(project_root, sources: list[DataSource], ...) — it takes
        # DataSource objects, not a source_names kwarg (which doesn't exist
        # on the real function at all and would raise TypeError). Use the
        # `source` object already fetched above.
        result = _run_sync(
            project_root=project_root,
            sources=[source],
            full_refresh=full_refresh,
        )
        return result if isinstance(result, dict) else {"status": "completed"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


@mcp.tool()
def run_transform(select: str | None = None, full_refresh: bool = False) -> dict[str, Any]:
    """Run dbt transformations. Equivalent to `dango run`.

    Args:
        select: dbt --select expression (e.g. "stg_stripe+", "marts"). Runs all if omitted.
        full_refresh: If True, rebuild incremental models from scratch.

    Returns dict with: status, output (dbt stdout), error (if failed).
    """
    project_root = _get_project_root()
    from dango.transformation import run_dbt_models
    from dango.utils import DbtLock

    # Single-writer DuckDB (VAL-003) — was the one run_dbt_models() caller
    # missing lock acquisition. Mirrors transform.py's run(); a lock timeout
    # (DbtLockError) falls through to the except below unchanged.
    lock = DbtLock(
        project_root=project_root,
        source="mcp",
        operation=f"run_transform select={select}" if select else "run_transform",
    )
    try:
        lock.acquire()
        # Correction (coordinating-chat pre-dispatch verification, 2026-09-03):
        # run_dbt_models() returns tuple[bool, str] (success, output), not a
        # single value — `if result` on a 2-tuple is always truthy regardless
        # of the bool inside it, so the original snippet here silently
        # reported every dbt failure as "completed". Unpack the tuple.
        success, output = run_dbt_models(project_root, select=select, full_refresh=full_refresh)
        return {"status": "completed" if success else "failed", "output": output}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        if lock._acquired:
            lock.release()


@mcp.tool()
def run_doctor() -> list[dict[str, Any]]:
    """Check credential health for all configured sources. Equivalent to `dango doctor`.

    Returns list of dicts with: source, type, status (ok/missing/expired), detail.
    """
    project_root = _get_project_root()
    # Correction (coordinating-chat pre-dispatch verification, 2026-09-03):
    # run_doctor_cached does not exist anywhere in the codebase (the CLI's
    # own `dango doctor` command, cli/commands/doctor.py, calls this
    # function directly — verified by reading it). Already returns the
    # exact list[dict[str, Any]] shape this tool's docstring promises.
    from dango.ingestion.credential_health import get_cached_credential_health

    return get_cached_credential_health(project_root)


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

    Returns dict with: status, source_name, next_steps (credentials to configure).
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

    return {
        "status": "created",
        "source_name": source_name,
        "source_type": source_type,
        "auth_type": auth_type,
        "next_steps": next_steps,
    }


@mcp.tool()
def list_source_types() -> list[dict[str, Any]]:
    """List all available source types in the Dango registry.

    Returns list of dicts with: type, name, description, auth_type, category.
    """
    from dango.ingestion.sources.registry import SOURCE_REGISTRY

    return [
        {
            "type": k,
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "auth_type": v.get("auth_type", "none"),
            "category": v.get("category", "other"),
        }
        for k, v in SOURCE_REGISTRY.items()
        if v.get("wizard_enabled", True)
    ]


@mcp.tool()
def create_model(
    model_name: str,
    layer: str,
    upstream_refs: list[str],
    description: str = "",
) -> dict[str, Any]:
    """Create a new dbt model with correct structure and naming conventions.

    Enforces Dango's data modelling best practices:
    - staging: one-to-one with raw source tables, light cleaning only
    - intermediate: reusable business logic, never referenced by BI tools directly
    - marts: final business metrics (fct_*, dim_*), optimised for dashboards

    Args:
        model_name: Model name. Must follow convention:
                    staging → stg_<source>__<entity> (double underscore)
                    intermediate → int_<description>
                    marts → fct_<metric> or dim_<entity>
        layer: One of "staging", "intermediate", "marts"
        upstream_refs: List of upstream model or source names to ref() in the SQL.
                       For staging: use source names. For others: use model names.
        description: What this model represents (written to schema.yml).

    Returns dict with: status, file_path, sql_scaffold, warnings (if any).
    """
    project_root = _get_project_root()

    # Validate layer
    if layer not in ("staging", "intermediate", "marts"):
        return {"error": f"layer must be 'staging', 'intermediate', or 'marts'. Got: '{layer}'"}

    # Reject path traversal / directory separators in model_name before it is
    # ever combined into a filesystem path below (regression-risk item flagged
    # in the session prompt: model_dir / f"{model_name}.sql" must stay inside
    # the project). Naming-convention checks below only constrain the prefix,
    # not the rest of the string, so this has to be its own explicit guard.
    if "/" in model_name or "\\" in model_name or ".." in model_name:
        return {"error": f"Invalid model_name: '{model_name}'. Must not contain path separators."}

    # Enforce naming conventions
    warnings = []
    if layer == "staging" and not model_name.startswith("stg_"):
        return {"error": "Staging models must be named stg_<source>__<entity> (starts with stg_)"}
    if layer == "intermediate" and not model_name.startswith("int_"):
        return {"error": "Intermediate models must be named int_<description> (starts with int_)"}
    if layer == "marts" and not (model_name.startswith("fct_") or model_name.startswith("dim_")):
        return {"error": "Marts models must be named fct_<metric> or dim_<entity>"}

    # Check for raw table refs in marts (anti-pattern)
    if layer == "marts":
        for ref in upstream_refs:
            if ref.startswith("raw_") or "__" not in ref:
                warnings.append(
                    f"Warning: '{ref}' looks like a raw table. Marts should ref staging/intermediate models, not raw tables."
                )

    # Determine model directory
    layer_dir_map = {
        "staging": project_root / "dbt" / "models" / "staging",
        "intermediate": project_root / "dbt" / "models" / "intermediate",
        "marts": project_root / "dbt" / "models" / "marts",
    }
    model_dir = layer_dir_map[layer]
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{model_name}.sql"

    if model_path.exists():
        return {"error": f"Model '{model_name}' already exists at {model_path}"}

    # Build ref() expressions
    #
    # Bug found during implementation (not one of the 5 corrections flagged
    # pre-dispatch): the staging branch below originally used only double
    # braces (f"{{ source(...) }}"), which an f-string collapses to a
    # *single* literal brace on each side ("{ source(...) }") — confirmed by
    # evaluating the expression directly. Jinja only recognizes double
    # braces ("{{ ... }}"); single braces are inert literal text, so every
    # staging model create_model() scaffolded would fail to compile in dbt
    # (the source() call would never resolve). Uses four braces per side
    # here, matching the already-correct ref() branch below, to collapse to
    # the required "{{ source(...) }}".
    ref_expressions = []
    for ref in upstream_refs:
        if layer == "staging":
            # Staging refs source tables
            ref_expressions.append(f"{{{{ source('{ref.split('_')[0]}', '{ref}') }}}}")
        else:
            ref_expressions.append(f"{{{{ ref('{ref}') }}}}")

    # Generate SQL scaffold
    if layer == "staging":
        sql = f"""with source as (
    select * from {ref_expressions[0] if ref_expressions else "{{ source('source_name', 'table_name') }}"}
),

renamed as (
    select
        -- TODO: rename and clean columns here
        *
    from source
)

select * from renamed
"""
    elif layer == "intermediate":
        joins = (
            "\n".join(
                f"    left join {r} using (id)  -- TODO: verify join key"
                for r in ref_expressions[1:]
            )
            if len(ref_expressions) > 1
            else "    -- TODO: add joins if needed"
        )
        sql = f"""with base as (
    select * from {ref_expressions[0] if ref_expressions else "{{ ref('stg_source__entity') }}"}
),

{joins}

-- TODO: add business logic here

select * from base
"""
    else:  # marts
        # Correction (coordinating-chat pre-dispatch verification, 2026-09-03): the original
        # snippet here nested an f-string with escaped quotes inside another f-string's {}
        # expression — a hard SyntaxError on Python <3.12 ("f-string expression part cannot
        # include a backslash"), confirmed by actually compiling it against this project's
        # own venv (3.11). This project's CI matrix includes Python 3.10, so this would have
        # broken the module at import time. Rewritten below with a plain helper function —
        # verified to compile and produce correct output before this file was finalized.
        def _ref_alias(ref_expr: str) -> str:
            return ref_expr.replace("{{ ref('", "").replace("') }}", "")

        if ref_expressions:
            ctes = ", ".join(f"{_ref_alias(r)} as (select * from {r})" for r in ref_expressions)
            first_alias = _ref_alias(ref_expressions[0])
        else:
            ctes = "base as (select 1 as placeholder)"
            first_alias = "base"

        sql = f"""with {ctes}

-- TODO: add aggregations and business metrics here

select * from {first_alias}
"""

    model_path.write_text(sql)

    # Update schema.yml
    _update_schema_yml(model_dir, model_name, description, upstream_refs)

    return {
        "status": "created",
        "file_path": str(model_path.relative_to(project_root)),
        "sql_scaffold": sql,
        "warnings": warnings,
        "next_steps": [
            f"Edit {model_path.name} to add your business logic",
            "Run: dango run to test",
        ],
    }


@mcp.tool()
def add_schedule(
    schedule_name: str,
    cron: str,
    sources: list[str],
    timezone: str = "UTC",
    skip_dbt: bool = False,
) -> dict[str, Any]:
    """Add a new sync schedule.

    Args:
        schedule_name: Unique name for this schedule (lowercase_with_underscores)
        cron: Cron expression (e.g. "0 7 * * *" for 7am daily).
              Common presets: "0 * * * *" (hourly), "0 7 * * *" (daily 7am),
              "0 7 * * 1-5" (weekdays 7am)
        sources: List of source names to sync on this schedule
        timezone: Timezone for the cron (e.g. "Asia/Singapore", "US/Eastern"). Default UTC.
        skip_dbt: If True, sync only — do not run dbt transforms after sync.

    Returns dict with: status, schedule_name, next_run_approx.
    """
    project_root = _get_project_root()
    from dango.config.helpers import load_config
    from dango.config.schedules import (
        ScheduleConfig,
        ScheduleType,
        load_schedules_config,
        save_schedules_config,
    )

    # Validate sources exist
    config = load_config(project_root)
    if config:
        for s in sources:
            if config.sources.get_source(s) is None:
                return {
                    "error": f"Source '{s}' not found in sources.yml. Add it first with add_source()."
                }

    # Load existing schedules
    schedules_config = load_schedules_config(project_root)
    existing_names = {s.name for s in schedules_config.schedules}
    if schedule_name in existing_names:
        return {"error": f"Schedule '{schedule_name}' already exists"}

    new_schedule = ScheduleConfig(
        name=schedule_name,
        cron=cron,
        sources=sources,
        timezone=timezone,
        type=ScheduleType.SYNC_ONLY if skip_dbt else ScheduleType.SYNC,
        enabled=True,
    )
    schedules_config.schedules.append(new_schedule)
    save_schedules_config(project_root, schedules_config)

    return {
        "status": "created",
        "schedule_name": schedule_name,
        "cron": cron,
        "timezone": timezone,
        "sources": sources,
        "next_steps": ["Run: dango schedule reload (or restart dango) to activate"],
    }


# ── Schema YML helper ─────────────────────────────────────────────────────────


def _update_schema_yml(
    model_dir: Path, model_name: str, description: str, upstream_refs: list[str]
) -> None:
    """Add model entry to schema.yml in the model's directory."""
    import yaml

    schema_path = model_dir / "schema.yml"
    if schema_path.exists():
        try:
            existing = yaml.safe_load(schema_path.read_text()) or {}
        except Exception:
            existing = {}
    else:
        existing = {"version": 2, "models": []}

    existing.setdefault("models", [])

    # Avoid duplicates
    if any(m.get("name") == model_name for m in existing["models"]):
        return

    existing["models"].append(
        {
            "name": model_name,
            "description": description or f"# TODO: Add description for {model_name}",
            "columns": [],  # Session G's validate check will flag this
        }
    )

    schema_path.write_text(yaml.dump(existing, default_flow_style=False, sort_keys=False))
