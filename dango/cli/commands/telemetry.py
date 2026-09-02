"""dango/cli/commands/telemetry.py

Unified telemetry control for Dango, dbt, dlt, and Metabase — a single
command surface that writes through to each provider's own real config
mechanism (Dango's ~/.dango/telemetry.json, a dbt subprocess env sentinel,
dlt's .dlt/config.toml, and Metabase's admin Setting API). This controls
telemetry for the components Dango configures; it does not claim anything
about network traffic outside those four providers.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.table import Table

from dango.cli import console

PROVIDERS = ("dango", "dbt", "dlt", "metabase")

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
    """Write the dbt telemetry sentinel file, read by `_dbt_telemetry_env()`
    in transformation/__init__.py to decide whether to inject
    DBT_SEND_ANONYMOUS_USAGE_STATS=false into the dbt subprocess env.

    Machine-level (~/.dango/), matching Dango's own telemetry identity scope
    (see dango/telemetry.py) — one opt-out covers every project on the
    machine.

    Raises:
        click.ClickException: If the sentinel file can't be written (e.g.
            ~/.dango is read-only or otherwise inaccessible) — converted
            from a raw OSError so `--all` can skip this provider and
            continue with the rest instead of crashing the whole command,
            matching the error-handling contract set_metabase_telemetry()
            uses for every one of its own failure modes.
    """
    sentinel = Path.home() / ".dango" / "dbt_telemetry"
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("true" if enabled else "false")
    except OSError as e:
        raise click.ClickException(f"Could not write dbt telemetry sentinel: {e}") from e


def _set_dlt_telemetry(enabled: bool, project_root: Path | None) -> None:
    """Write dlthub_telemetry to .dlt/config.toml under [runtime].

    Writes a native TOML boolean (not a string) to match the value type
    dlt's own `dango telemetry`-equivalent CLI (`dlt telemetry switch`)
    writes — RuntimeConfiguration.dlthub_telemetry is a bool-typed field.

    Raises:
        click.ClickException: If project_root is None, if the file can't
            be read/written (OSError — e.g. permissions), or if the
            existing .dlt/config.toml is malformed
            (tomlkit.exceptions.TOMLKitError, e.g. from manual editing) —
            the latter two converted from raw exceptions so `--all` can
            skip this provider and continue, same contract as dbt above
            and Metabase's set_metabase_telemetry().
    """
    import tomlkit
    from tomlkit.exceptions import TOMLKitError

    if project_root is None:
        raise click.ClickException("Must be run inside a Dango project for dlt control")
    config_path = project_root / ".dlt" / "config.toml"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
        if "runtime" not in doc:
            doc.add("runtime", tomlkit.table())
        doc["runtime"]["dlthub_telemetry"] = enabled
        config_path.write_text(tomlkit.dumps(doc))
    except OSError as e:
        raise click.ClickException(f"Could not write .dlt/config.toml: {e}") from e
    except TOMLKitError as e:
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
    """Return dbt's current opt-in state per the sentinel file (default: on)."""
    sentinel = Path.home() / ".dango" / "dbt_telemetry"
    if sentinel.exists():
        return sentinel.read_text().strip() == "true"
    return True  # default on


def _get_dlt_telemetry_state(project_root: Path | None) -> bool:
    """Return dlt's current opt-in state per .dlt/config.toml (default: on)."""
    if project_root is None:
        return True
    config_path = project_root / ".dlt" / "config.toml"
    if not config_path.exists():
        return True
    try:
        import tomlkit

        doc = tomlkit.parse(config_path.read_text())
        val = doc.get("runtime", {}).get("dlthub_telemetry", True)
        return bool(val)
    except Exception:
        return True


def _get_metabase_telemetry_state(project_root: Path | None) -> bool:
    """Return Metabase's last-known opt-in state.

    Reads the local cache file `set_metabase_telemetry()` writes after each
    successful live API call (dango/visualization/metabase.py) — this
    reports the real last-set state without requiring Metabase to be
    running just to print a status table. If telemetry was never toggled
    through this command (no cache file, or no project), defaults to "on":
    that's Metabase's own out-of-the-box default for anon-tracking-enabled.
    """
    if project_root is None:
        return True
    state_file = project_root / ".dango" / "metabase_telemetry_state"
    if not state_file.exists():
        return True
    return state_file.read_text().strip() == "true"
