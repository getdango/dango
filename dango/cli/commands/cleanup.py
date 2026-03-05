"""dango/cli/commands/cleanup.py

Cleanup old logs, dbt artifacts, and Python cache.
"""

from __future__ import annotations

from pathlib import Path

import click

from dango.cli import console


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.1f} MB"
    else:
        return f"{size_bytes / 1024**3:.2f} GB"


def _collect_log_archives(log_dir: Path, max_age_days: int) -> list[tuple[Path, int]]:
    """Find compressed log archives older than *max_age_days*.

    Args:
        log_dir: Directory containing log archives.
        max_age_days: Maximum age in days before an archive is eligible.

    Returns:
        List of (path, size_bytes) for each expired archive.
    """
    from datetime import datetime, timezone

    if not log_dir.exists():
        return []

    cutoff_ts = datetime.now(timezone.utc).timestamp() - (max_age_days * 86_400)
    results: list[tuple[Path, int]] = []

    for archive in log_dir.glob("*.jsonl.gz"):
        try:
            stat = archive.stat()
            if stat.st_mtime < cutoff_ts:
                results.append((archive, stat.st_size))
        except OSError:
            continue

    return results


def _collect_dbt_artifacts(project_root: Path) -> list[tuple[Path, int]]:
    """Find dbt build artifacts (target/ and logs/ directories).

    Args:
        project_root: Project root directory.

    Returns:
        List of (directory_path, total_size_bytes) for each existing directory.
    """
    results: list[tuple[Path, int]] = []

    for subdir in ("target", "logs"):
        dbt_dir = project_root / "dbt" / subdir
        if not dbt_dir.is_dir():
            continue

        total_size = 0
        for path in dbt_dir.rglob("*"):
            if path.is_file():
                try:
                    total_size += path.stat().st_size
                except OSError:
                    continue

        results.append((dbt_dir, total_size))

    return results


_SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", ".tox"}


def _collect_pycache(project_root: Path) -> list[tuple[Path, int]]:
    """Find __pycache__ directories under the project root.

    Skips virtual environments, node_modules, and .git directories to avoid
    unnecessary traversal and churn.

    Args:
        project_root: Project root directory.

    Returns:
        List of (directory_path, total_size_bytes) for each __pycache__ dir.
    """
    results: list[tuple[Path, int]] = []
    seen_parents: set[Path] = set()

    for cache_dir in project_root.rglob("__pycache__"):
        if not cache_dir.is_dir():
            continue

        # Skip directories inside venv, .git, etc.
        parts = cache_dir.relative_to(project_root).parts
        if _SKIP_DIRS.intersection(parts):
            continue

        # Skip nested __pycache__ already covered by a parent
        if any(cache_dir.is_relative_to(parent) for parent in seen_parents):
            continue

        total_size = 0
        for path in cache_dir.rglob("*"):
            if path.is_file():
                try:
                    total_size += path.stat().st_size
                except OSError:
                    continue

        results.append((cache_dir, total_size))
        seen_parents.add(cache_dir)

    return results


@click.command("cleanup")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--logs-only", is_flag=True, help="Only clean log archives, skip dbt/cache.")
@click.pass_context
def cleanup(ctx: click.Context, dry_run: bool, yes: bool, logs_only: bool) -> None:
    """Remove old log archives, dbt artifacts, and Python cache.

    Cleans up disk space by removing:

    \b
      - Compressed log archives older than 90 days (.jsonl.gz)
      - dbt build artifacts (dbt/target/, dbt/logs/)
      - Python bytecode cache (__pycache__/ directories)

    Examples:

    \b
      dango cleanup              Clean all with confirmation
      dango cleanup --dry-run    Show what would be deleted
      dango cleanup --yes        Clean without confirmation
      dango cleanup --logs-only  Only clean old log archives
    """
    import shutil

    from dango.utils.db_health import get_disk_usage_summary
    from dango.utils.log_rotation import cleanup_old_archives, get_log_disk_usage

    from ..utils import require_project_context

    console.print("[bold]Cleanup[/bold]\n")

    try:
        project_root = require_project_context(ctx)
        log_dir = project_root / ".dango" / "logs"
        max_age_days = 90

        # --- Capture before state ---
        disk_before = get_disk_usage_summary(project_root)
        log_usage_before = get_log_disk_usage(log_dir)

        # --- Collect items to clean ---
        log_archives = _collect_log_archives(log_dir, max_age_days)
        dbt_artifacts: list[tuple[Path, int]] = []
        pycache_dirs: list[tuple[Path, int]] = []

        if not logs_only:
            dbt_artifacts = _collect_dbt_artifacts(project_root)
            pycache_dirs = _collect_pycache(project_root)

        log_total = sum(size for _, size in log_archives)
        dbt_total = sum(size for _, size in dbt_artifacts)
        cache_total = sum(size for _, size in pycache_dirs)
        grand_total = log_total + dbt_total + cache_total

        # --- Nothing to clean ---
        if grand_total == 0:
            console.print("[green]Nothing to clean up.[/green]")
            return

        # --- Display summary ---
        from rich.table import Table

        table = Table(title="Items to remove", show_header=True, header_style="bold cyan")
        table.add_column("Category", style="white")
        table.add_column("Items", justify="right")
        table.add_column("Size", justify="right")

        if log_archives:
            table.add_row(
                "Log archives (>90 days)",
                str(len(log_archives)),
                _format_size(log_total),
            )
        if dbt_artifacts:
            dirs = ", ".join(p.name for p, _ in dbt_artifacts)
            table.add_row(
                f"dbt artifacts ({dirs})", str(len(dbt_artifacts)), _format_size(dbt_total)
            )
        if pycache_dirs:
            table.add_row("Python cache", str(len(pycache_dirs)), _format_size(cache_total))

        table.add_section()
        table.add_row("[bold]Total[/bold]", "", f"[bold]{_format_size(grand_total)}[/bold]")

        console.print(table)
        console.print()

        # --- Dry run ---
        if dry_run:
            if log_archives:
                console.print("[dim]Log archives:[/dim]")
                for path, size in log_archives:
                    console.print(f"  {path.name}  ({_format_size(size)})")
            if dbt_artifacts:
                console.print("[dim]dbt directories:[/dim]")
                for path, size in dbt_artifacts:
                    console.print(f"  {path.relative_to(project_root)}/  ({_format_size(size)})")
            if pycache_dirs:
                console.print("[dim]Python cache directories:[/dim]")
                for path, size in pycache_dirs:
                    console.print(f"  {path.relative_to(project_root)}/  ({_format_size(size)})")
            console.print()
            console.print("[yellow]Dry run — no files deleted.[/yellow]")
            return

        # --- Confirmation ---
        if not yes:
            if not click.confirm(f"Delete {_format_size(grand_total)} of files?"):
                console.print("[yellow]Cancelled.[/yellow]")
                return

        # --- Perform cleanup ---
        freed = 0

        # Log archives
        if log_archives:
            log_size_before = get_log_disk_usage(log_dir)["total_bytes"]
            cleanup_old_archives(log_dir, "*.jsonl.gz", max_age_days)
            log_size_after = get_log_disk_usage(log_dir)["total_bytes"]
            log_freed = max(0, log_size_before - log_size_after)
            freed += log_freed
            console.print(
                f"[green]\u2713[/green] Removed {len(log_archives)} log archive(s)"
                f" ({_format_size(log_freed)})"
            )

        # dbt artifacts
        for path, size in dbt_artifacts:
            try:
                shutil.rmtree(path)
                freed += size
                console.print(
                    f"[green]\u2713[/green] Removed {path.relative_to(project_root)}/"
                    f" ({_format_size(size)})"
                )
            except OSError as e:
                console.print(
                    f"[red]\u2717[/red] Failed to remove {path.relative_to(project_root)}/: {e}"
                )

        # Python cache
        cache_removed = 0
        cache_freed = 0
        for path, size in pycache_dirs:
            if not path.exists():
                # Already removed as child of a parent __pycache__
                continue
            try:
                shutil.rmtree(path)
                cache_freed += size
                cache_removed += 1
            except OSError:
                continue

        freed += cache_freed
        if cache_removed:
            console.print(
                f"[green]\u2713[/green] Removed {cache_removed} __pycache__ dir(s)"
                f" ({_format_size(cache_freed)})"
            )

        # --- After summary ---
        console.print()
        disk_after = get_disk_usage_summary(project_root)
        log_usage_after = get_log_disk_usage(log_dir)

        console.print("[bold]Disk usage:[/bold]")
        console.print(
            f"  System: {disk_before['used_gb']}GB used → {disk_after['used_gb']}GB used"
            f"  ({disk_after['free_gb']}GB free)"
        )
        console.print(
            f"  Logs:   {_format_size(log_usage_before['total_bytes'])}"
            f" → {_format_size(log_usage_after['total_bytes'])}"
        )
        console.print()
        console.print(f"[green]Freed {_format_size(freed)}.[/green]")

    except click.Abort:
        raise
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
