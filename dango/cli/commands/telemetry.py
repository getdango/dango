"""dango/cli/commands/telemetry.py

Unified telemetry control for Dango, dbt, dlt, and Metabase — a single
command surface that writes through to each provider's own real config
mechanism (Dango's ~/.dango/telemetry.json, dbt's dbt_telemetry key in
~/.dango/config.yml, dlt's .dlt/config.toml, and Metabase's admin Setting
API). This controls
telemetry for the components Dango configures; it does not claim anything
about network traffic outside those four providers.

The `/settings/telemetry` web page (`web/routes/telemetry.py`, 1.0.8-U) is a
second front-end onto the same underlying state. The actual state
read/write logic for dbt and dlt lives in `dango/telemetry.py` (Level 0)
and for Metabase in `dango/visualization/metabase.py` (Level 2) —
`web/routes/telemetry.py` (Level 2) cannot import this module (Level 3),
so the provider helpers below are thin wrappers that call the relocated
functions and translate their plain exceptions into `click.ClickException`
for CLI-friendly error output.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.table import Table

from dango.cli import console
from dango.telemetry import PROVIDERS

# Providers whose write-through requires an active Dango project (project_root
# is not None). Used by the --all path to downgrade a missing-project failure
# to a warning instead of aborting the whole command — dango and dbt are
# machine-level and always succeed regardless of project context.
_PROJECT_SCOPED_PROVIDERS = ("dlt", "metabase")


@click.group("telemetry")
def telemetry() -> None:
    """View and control telemetry for Dango and bundled tools."""


@telemetry.command("status")
@click.pass_context
def telemetry_status(ctx: click.Context) -> None:
    """Show telemetry state for all four providers."""
    from dango.telemetry import is_telemetry_enabled

    project_root = ctx.obj.get("project_root")

    table = Table(title="Telemetry Status", show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("State")
    table.add_column("What it sends")
    table.add_column("Control")

    # Dango
    dango_on = is_telemetry_enabled()
    table.add_row(
        "dango",
        "[green]on[/green]" if dango_on else "[dim]off[/dim]",
        "UUID, version, OS, source type names",
        "dango telemetry on/off --provider dango",
    )

    # dbt
    dbt_on = _get_dbt_telemetry_state()
    table.add_row(
        "dbt-core",
        "[green]on[/green]" if dbt_on else "[dim]off[/dim]",
        "OS, Python version, invocation success/duration",
        "dango telemetry on/off --provider dbt",
    )

    # dlt
    dlt_on = _get_dlt_telemetry_state(project_root)
    table.add_row(
        "dlt",
        "[green]on[/green]" if dlt_on else "[dim]off[/dim]",
        "Command names, hashed pipeline names, execution times",
        "dango telemetry on/off --provider dlt",
    )

    # Metabase
    mb_on = _get_metabase_telemetry_state(project_root)
    table.add_row(
        "metabase",
        "[green]on[/green]" if mb_on else "[dim]off[/dim]",
        "Anonymous usage statistics",
        "dango telemetry on/off --provider metabase",
    )

    console.print()
    console.print(table)
    console.print(
        "\n[dim]controls telemetry for the components Dango configures — "
        "full egress docs: docs/network-egress.yml[/dim]\n"
    )


@telemetry.command("off")
@click.option("--all", "all_providers", is_flag=True, help="Disable all providers")
@click.option("--provider", type=click.Choice(PROVIDERS), help="Disable specific provider")
@click.pass_context
def telemetry_off(ctx: click.Context, all_providers: bool, provider: str | None) -> None:
    """Disable telemetry for one or all providers."""
    _run_toggle(ctx, all_providers, provider, enabled=False)


@telemetry.command("on")
@click.option("--all", "all_providers", is_flag=True, help="Enable all providers")
@click.option("--provider", type=click.Choice(PROVIDERS), help="Enable specific provider")
@click.pass_context
def telemetry_on(ctx: click.Context, all_providers: bool, provider: str | None) -> None:
    """Enable telemetry for one or all providers."""
    _run_toggle(ctx, all_providers, provider, enabled=True)


def _run_toggle(
    ctx: click.Context, all_providers: bool, provider: str | None, enabled: bool
) -> None:
    """Shared implementation for the `on` and `off` subcommands.

    In --all mode, a provider that requires a project (dlt, metabase) and
    finds none is downgraded to a warning rather than aborting the rest of
    the run — dango and dbt are machine-level and must still succeed. An
    explicit --provider request that can't be fulfilled still raises, since
    that is a single deliberate action rather than a best-effort sweep.
    """
    targets: list[str]
    if all_providers:
        targets = list(PROVIDERS)
    elif provider:
        targets = [provider]
    else:
        raise click.UsageError("Specify --all or --provider PROVIDER")

    project_root = ctx.obj.get("project_root")
    verb = "enabled" if enabled else "disabled"

    for p in targets:
        if all_providers and p in _PROJECT_SCOPED_PROVIDERS and project_root is None:
            console.print(f"[yellow]![/yellow] {p}: skipped — not inside a Dango project")
            continue
        try:
            _set_provider(p, enabled=enabled, project_root=project_root)
        except click.ClickException as exc:
            if all_providers:
                console.print(f"[yellow]![/yellow] {p}: skipped — {exc.format_message()}")
                continue
            raise
        console.print(f"[green]✓[/green] {p}: telemetry {verb}")

    console.print()


# ── Provider helpers ─────────────────────────────────────────────────────────────


def _set_provider(provider: str, enabled: bool, project_root: Path | None) -> None:
    """Dispatch a single provider's write-through. Raises click.ClickException on failure."""
    if provider == "dango":
        from dango.telemetry import set_telemetry_enabled

        set_telemetry_enabled(enabled)

    elif provider == "dbt":
        _set_dbt_telemetry(enabled)

    elif provider == "dlt":
        _set_dlt_telemetry(enabled, project_root)

    elif provider == "metabase":
        _set_metabase_telemetry(enabled, project_root)


def _set_dbt_telemetry(enabled: bool) -> None:
    """Write dbt's opt-out state to ~/.dango/config.yml under the dbt_telemetry key.

    Thin wrapper (1.0.8-U) around `dango.telemetry.set_dbt_telemetry_state()`
    — the actual read/write logic lives there (Level 0) so
    `web/routes/telemetry.py` (Level 2) can call it too without importing
    this Level-3 module. This wrapper's only job is translating that
    function's plain `OSError` into the `click.ClickException` this CLI's
    `--all` handling expects.

    Raises:
        click.ClickException: If the write fails (e.g. permission denied,
            disk full) — same message text as before the 1.0.8-U
            relocation, so `--all` can skip this provider and continue,
            matching the error-handling contract `set_metabase_telemetry()`
            uses.
    """
    from dango.telemetry import set_dbt_telemetry_state

    try:
        set_dbt_telemetry_state(enabled)
    except OSError:
        raise click.ClickException("Could not write ~/.dango/config.yml") from None


def _set_dlt_telemetry(enabled: bool, project_root: Path | None) -> None:
    """Write dlthub_telemetry to .dlt/config.toml under [runtime].

    Thin wrapper (1.0.8-U) around `dango.telemetry.set_dlt_telemetry_state()`
    — the actual read/write logic lives there (Level 0) so
    `web/routes/telemetry.py` (Level 2) can call it too without importing
    this Level-3 module. This wrapper's only job is the `project_root is
    None` check (CLI-specific — the web route always has a project_root)
    and translating that function's plain exceptions into
    `click.ClickException` with identical message text to before the
    relocation.

    Raises:
        click.ClickException: If project_root is None, if the file can't
            be read/written (OSError — e.g. permissions), or if the
            existing .dlt/config.toml is malformed (a ValueError wrapping
            tomlkit.exceptions.TOMLKitError, e.g. from manual editing) —
            the latter two converted from raw exceptions so `--all` can
            skip this provider and continue, same contract as dbt above
            and Metabase's set_metabase_telemetry().
    """
    from dango.telemetry import set_dlt_telemetry_state

    if project_root is None:
        raise click.ClickException("Must be run inside a Dango project for dlt control")
    try:
        set_dlt_telemetry_state(project_root, enabled)
    except OSError as e:
        raise click.ClickException(f"Could not write .dlt/config.toml: {e}") from e
    except ValueError as e:
        raise click.ClickException(f"Could not parse .dlt/config.toml: {e}") from e


def _set_metabase_telemetry(enabled: bool, project_root: Path | None) -> None:
    """Toggle Metabase anonymous tracking via the admin Setting API.

    The "is Metabase configured" check lives once, in
    `set_metabase_telemetry()` itself (dango/visualization/metabase.py) —
    not duplicated here — since that's the function that actually reads
    `.dango/metabase.yml`.
    """
    if project_root is None:
        raise click.ClickException("Must be run inside a Dango project for Metabase control")

    from dango.visualization.metabase import set_metabase_telemetry

    set_metabase_telemetry(project_root, enabled)


def _get_dbt_telemetry_state() -> bool:
    """Return dbt's current opt-in state per ~/.dango/config.yml (default: on).

    Thin wrapper (1.0.8-U) — the actual read logic now lives in
    `dango.telemetry.get_dbt_telemetry_state()` (Level 0).
    """
    from dango.telemetry import get_dbt_telemetry_state

    return get_dbt_telemetry_state()


def _get_dlt_telemetry_state(project_root: Path | None) -> bool:
    """Return dlt's current opt-in state per .dlt/config.toml (default: on).

    Thin wrapper (1.0.8-U) — the actual read logic now lives in
    `dango.telemetry.get_dlt_telemetry_state()` (Level 0).
    """
    from dango.telemetry import get_dlt_telemetry_state

    return get_dlt_telemetry_state(project_root)


def _get_metabase_telemetry_state(project_root: Path | None) -> bool:
    """Return Metabase's last-known opt-in state.

    Thin wrapper (1.0.8-U) — the actual read logic now lives in
    `dango.visualization.metabase.get_metabase_telemetry_state()` (Level 2,
    same level as `web/`), reading the local cache file
    `set_metabase_telemetry()` writes after each successful live API call.
    """
    from dango.visualization.metabase import get_metabase_telemetry_state

    return get_metabase_telemetry_state(project_root)
