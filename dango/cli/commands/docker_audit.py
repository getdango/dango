"""dango/cli/commands/docker_audit.py

Machine-wide diagnostic + cleanup for dango-managed Docker resources.

Added by 1.0.8-Q8 after a real incident (2026-09-09, see
v1.0.x-planning/1.0.8/BUGS-FOUND.md and dango/exceptions.py's
DockerIdentityCollisionError): get_compose_project_name() derives a
project's Docker Compose identity from an MD5 hash of its path string,
which is not a stable identifier. When a scratch project's containers
looked orphaned, a human/agent operator guessed which real project they
belonged to and ran manual `docker compose down` + `docker volume rm` +
`docker rmi` outside any `dango` command — destroying a different, real
project's Metabase data.

This command is the tool that should have existed: it reads Docker
Compose's own `com.docker.compose.project.working_dir` label (the literal
path used to create each container) instead of guessing, groups every
dango-* resource on the machine by that ground truth, and only ever offers
cleanup for the unambiguously-orphaned group.

Deliberately NOT part of `dango doctor` (cli/commands/doctor.py), which is
scoped specifically to credential health.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.table import Table

from dango.cli import console
from dango.cli.utils import safe_confirm

# Docker Compose's default naming conventions (live-verified against Docker
# 28.5.1 / Compose v2.40.2 with a scratch compose project — see PR
# description for the exact commands):
#   - Named volume "foo" under project "dango-<hash>"  -> "dango-<hash>_foo"
#   - A locally *built* service image (no explicit `image:` key) for
#     service "metabase" under project "dango-<hash>"  -> "dango-<hash>-metabase"
# dbt-docs uses the public `nginx:alpine` image directly (see
# templates/docker-compose.yml.j2) and is never tagged with a dango-*
# prefix, but the suffix is checked anyway for forward/cloud-template
# compatibility — it is harmless if it never matches.
_IMAGE_SUFFIXES = ("-metabase", "-dbt-docs")


@dataclass
class ResourceGroup:
    """Every dango-* Docker resource sharing one Compose project name."""

    project: str
    working_dirs: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    classification: str = ""  # "orphaned" | "needs_attention" | "live"


def _list_dango_containers() -> list[dict[str, str]]:
    """List every container (running or stopped), machine-wide, whose
    com.docker.compose.project label starts with 'dango-'.

    Returns [] if Docker is unavailable or the check fails — this is a
    diagnostic listing, not a safety gate, so it fails open like the
    identity guard in platform/docker.py.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project",
                "--format",
                '{{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project"}}\t'
                '{{.Label "com.docker.compose.project.working_dir"}}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    records = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        container_id, name, project, working_dir = parts
        if not project.startswith("dango-"):
            continue
        records.append(
            {"id": container_id, "name": name, "project": project, "working_dir": working_dir}
        )
    return records


def _list_dango_volumes() -> list[str]:
    """List every volume name, machine-wide, starting with 'dango-'."""
    try:
        result = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [v.strip() for v in result.stdout.splitlines() if v.strip().startswith("dango-")]


def _list_dango_images() -> list[str]:
    """List every image repository name, machine-wide, starting with 'dango-'."""
    try:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [i.strip() for i in result.stdout.splitlines() if i.strip().startswith("dango-")]


def _project_from_resource_name(name: str) -> str | None:
    """Derive the compose project name embedded in a dango-prefixed volume
    or image name.

    Volumes and images don't carry the working_dir label directly (only
    containers do) — but Compose bakes the project name into their names
    (``dango-<hash>_metabase-data`` for volumes, ``dango-<hash>-metabase``
    for a locally-built image), so the project name can be recovered from
    the name itself and cross-referenced against any container sharing it.
    Returns None if the name doesn't match either known pattern.
    """
    if "_" in name:
        candidate = name.rsplit("_", 1)[0]
        if candidate.startswith("dango-"):
            return candidate
    for suffix in _IMAGE_SUFFIXES:
        if name.endswith(suffix):
            candidate = name[: -len(suffix)]
            if candidate.startswith("dango-"):
                return candidate
    return None


def _build_groups() -> list[ResourceGroup]:
    """Collect every dango-* Docker resource machine-wide, grouped by
    compose project name, and classify each group.

    Classification (deliberately conservative — "if in doubt, needs
    attention, never safe to clean"):
      - No container exists for this project name (a bare orphaned volume
        or image only): we have no working_dir label to verify against at
        all -> "needs_attention".
      - Containers exist but report more than one distinct working_dir
        value for the same project name: this is the literal signature of
        an identity collision (two different directories' containers
        sharing one Compose project name at different points in time)
        -> "needs_attention".
      - Containers exist with exactly one working_dir value:
          - that directory no longer exists on disk -> "orphaned" (safe to
            offer for cleanup)
          - that directory exists on disk -> "live" (a real project;
            display only, never offer to remove it)
    """
    groups: dict[str, ResourceGroup] = {}

    def _get(project: str) -> ResourceGroup:
        if project not in groups:
            groups[project] = ResourceGroup(project=project)
        return groups[project]

    for c in _list_dango_containers():
        g = _get(c["project"])
        g.containers.append(c["name"])
        if c["working_dir"] and c["working_dir"] not in g.working_dirs:
            g.working_dirs.append(c["working_dir"])

    for v in _list_dango_volumes():
        project = _project_from_resource_name(v)
        if project:
            _get(project).volumes.append(v)

    for i in _list_dango_images():
        project = _project_from_resource_name(i)
        if project:
            _get(project).images.append(i)

    for g in groups.values():
        if not g.containers:
            g.classification = "needs_attention"
        elif len(g.working_dirs) > 1:
            g.classification = "needs_attention"
        else:
            working_dir = g.working_dirs[0]
            g.classification = "orphaned" if not Path(working_dir).exists() else "live"

    return sorted(groups.values(), key=lambda g: g.project)


def _remove_group(group: ResourceGroup) -> None:
    """Remove every container/volume/image in an orphaned group.

    Caller (docker_audit()) guarantees this is only ever called for groups
    classified "orphaned" — never "needs_attention" or "live".
    """
    if group.containers:
        try:
            result = subprocess.run(
                ["docker", "rm", "-f", *group.containers],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                console.print(
                    f"[green]✓[/green] Removed {len(group.containers)} "
                    f"container(s) for {group.project}"
                )
            else:
                console.print(
                    f"[yellow]![/yellow] Could not remove containers for "
                    f"{group.project}: {result.stderr.strip()}"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            console.print(f"[red]✗[/red] Failed to remove containers for {group.project}: {e}")

    for vol in group.volumes:
        try:
            result = subprocess.run(
                ["docker", "volume", "rm", vol], capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                console.print(f"[green]✓[/green] Removed volume {vol}")
            else:
                console.print(
                    f"[yellow]![/yellow] Could not remove volume {vol}: {result.stderr.strip()}"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            console.print(f"[red]✗[/red] Failed to remove volume {vol}: {e}")

    for img in group.images:
        try:
            result = subprocess.run(
                ["docker", "rmi", img], capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                console.print(f"[green]✓[/green] Removed image {img}")
            else:
                console.print(
                    f"[yellow]![/yellow] Could not remove image {img}: {result.stderr.strip()}"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            console.print(f"[red]✗[/red] Failed to remove image {img}: {e}")


_STATUS_STYLE = {
    "orphaned": "[yellow]Orphaned (safe to clean)[/yellow]",
    "needs_attention": "[red]Needs attention[/red]",
    "live": "[green]Live[/green]",
}


@click.command("docker-audit")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt before cleaning up.")
@click.option(
    "--dry-run", is_flag=True, help="List and classify resources only; never offer cleanup."
)
def docker_audit(yes: bool, dry_run: bool) -> None:
    """List every dango-managed Docker resource on this machine and classify it.

    Groups every ``dango-*`` container, volume, and image on the machine
    (not just the current project) by their real Docker Compose project
    identity, using Compose's own
    ``com.docker.compose.project.working_dir`` label as ground truth rather
    than guessing from names or hashes.

    \b
      - Orphaned:        working directory no longer exists — safe to clean.
      - Needs attention: ambiguous (no container to verify against, or
                          conflicting working_dir labels under one project
                          name) — never auto-cleaned, investigate manually.
      - Live:             working directory exists — a real project, shown
                          for visibility only, never offered for removal.

    Only the orphaned group can ever be cleaned, and only behind an
    explicit confirmation prompt (or --yes).

    Examples:

    \b
      dango docker-audit              List, classify, and offer cleanup
      dango docker-audit --dry-run    List and classify only
      dango docker-audit --yes        Clean up orphaned resources without prompting
    """
    console.print("🍡 [bold]Docker resource audit[/bold]\n")

    groups = _build_groups()
    if not groups:
        console.print("[green]No dango-* Docker resources found on this machine.[/green]")
        return

    table = Table(title="Dango Docker resources", show_header=True, header_style="bold cyan")
    table.add_column("Project", style="bold")
    table.add_column("Working directory")
    table.add_column("Containers", justify="right")
    table.add_column("Volumes", justify="right")
    table.add_column("Images", justify="right")
    table.add_column("Status")

    for g in groups:
        if len(g.working_dirs) == 1:
            wd_display = g.working_dirs[0]
        elif g.working_dirs:
            wd_display = "[red](multiple/conflicting)[/red]"
        else:
            wd_display = "[dim]unknown (no container)[/dim]"

        table.add_row(
            g.project,
            wd_display,
            str(len(g.containers)),
            str(len(g.volumes)),
            str(len(g.images)),
            _STATUS_STYLE.get(g.classification, g.classification),
        )

    console.print(table)
    console.print()

    orphaned = [g for g in groups if g.classification == "orphaned"]
    needs_attention = [g for g in groups if g.classification == "needs_attention"]

    if needs_attention:
        console.print(
            f"[red]{len(needs_attention)} group(s) need attention[/red] — ambiguous identity, "
            "never auto-cleaned. Investigate manually, e.g.:\n"
            "  [dim]docker ps -a --filter label=com.docker.compose.project=<name>[/dim]\n"
        )

    if not orphaned:
        console.print("[dim]No unambiguously orphaned resources to clean up.[/dim]")
        return

    console.print(
        f"[yellow]{len(orphaned)} group(s) are orphaned[/yellow] "
        "(working directory no longer exists on disk):"
    )
    for g in orphaned:
        console.print(
            f"  {g.project}: {len(g.containers)} container(s), "
            f"{len(g.volumes)} volume(s), {len(g.images)} image(s)"
        )
    console.print()

    if dry_run:
        console.print("[dim]Dry run — re-run without --dry-run to clean these up.[/dim]")
        return

    if not yes and not safe_confirm(f"Remove {len(orphaned)} orphaned resource group(s)?"):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    for g in orphaned:
        _remove_group(g)

    console.print("\n[green]Cleanup complete.[/green]")
