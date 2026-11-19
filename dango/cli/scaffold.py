"""dango/cli/scaffold.py

The files a Dango project needs from `dango init`, and one check that `start`, `sync`, `run` and `validate` share.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.markup import escape

from dango.cli import console

# What a missing item means:
#   required  Dango cannot rebuild it (it holds the user's own settings or edits): say so, stop.
#   restore   a user-editable copy of a packaged template: recreated only after the user agrees
#             (`dango start --yes` or a yes at the prompt), and never over an existing file.
#   render    rewritten from project.yml before every `dango start`: nothing to do, only reported.
#   auto      an empty directory (or empty credential templates): recreated silently when missing.
REQUIRED = "required"
RESTORE = "restore"
RENDER = "render"
AUTO = "auto"

_VCS_FIX = "Restore it from version control or a backup of the project."


@dataclass(frozen=True)
class ScaffoldItem:
    """One path `dango init` creates, the commands that need it, and what to do if it is gone."""

    path: str
    kind: str  # "file" or "dir"
    commands: tuple[str, ...]
    action: str
    fix: str


# Every command named here also runs `require_project_context` first. `.dango/sources.yml` is
# listed with no commands: the config loader treats it as optional (no file = no sources).
SCAFFOLD_ITEMS: tuple[ScaffoldItem, ...] = (
    ScaffoldItem(
        ".dango/project.yml",
        "file",
        ("start", "sync", "run", "validate", "web"),
        REQUIRED,
        _VCS_FIX,
    ),
    ScaffoldItem(".dango/sources.yml", "file", (), REQUIRED, _VCS_FIX),
    ScaffoldItem(
        "docker-compose.yml",
        "file",
        ("start", "validate"),
        RENDER,
        "'dango start' recreates it from .dango/project.yml.",
    ),
    ScaffoldItem(
        "Dockerfile.metabase",
        "file",
        ("start", "validate"),
        RESTORE,
        "Run 'dango start --yes' to recreate it from Dango's packaged template "
        "(an existing file is never overwritten).",
    ),
    ScaffoldItem(
        "entrypoint.sh",
        "file",
        ("start", "validate"),
        RESTORE,
        "Run 'dango start --yes' to recreate it from Dango's packaged template "
        "(an existing file is never overwritten).",
    ),
    ScaffoldItem("dbt/dbt_project.yml", "file", ("sync", "run", "validate"), REQUIRED, _VCS_FIX),
    ScaffoldItem("dbt/profiles.yml", "file", ("sync", "run", "validate"), REQUIRED, _VCS_FIX),
    ScaffoldItem(
        "data",
        "dir",
        ("start", "sync"),
        AUTO,
        "'dango start' and 'dango sync' recreate this empty directory.",
    ),
    ScaffoldItem(
        ".dlt",
        "dir",
        ("sync", "validate"),
        AUTO,
        "'dango sync' recreates it with empty credential templates.",
    ),
)

_TEMPLATE_FILES = ("Dockerfile.metabase", "entrypoint.sh")


@dataclass(frozen=True)
class ScaffoldProblem:
    """A missing or unusable scaffold path, with the exact fix in `message`."""

    item: ScaffoldItem
    message: str
    regenerable: bool


@dataclass
class ScaffoldReport:
    """Result of `check_scaffold`: what is missing and what exists but cannot be used."""

    missing: list[ScaffoldProblem] = field(default_factory=list)
    unreadable: list[ScaffoldProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing is missing or unusable."""
        return not self.missing and not self.unreadable


def _unusable(path: Path, kind: str) -> bool:
    """True when something is at `path` but it is not a readable file / directory."""
    if kind == "dir":
        return not path.is_dir()
    if not path.is_file():
        return True
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return True
    return False


def check_scaffold(project_root: Path, for_command: str) -> ScaffoldReport:
    """Report the scaffold paths `for_command` needs that are missing or unusable. Writes nothing."""
    report = ScaffoldReport()
    for item in SCAFFOLD_ITEMS:
        if for_command not in item.commands:
            continue
        path = project_root / item.path
        if path.exists():
            if _unusable(path, item.kind):
                what = "a regular file" if item.kind == "file" else "a directory"
                text = (
                    f"{item.path}: exists but is not {what} or cannot be read. "
                    "Move or remove it, or fix its permissions, then try again."
                )
                report.unreadable.append(ScaffoldProblem(item, text, False))
        elif os.path.lexists(path):
            text = (
                f"{item.path}: is a link to a path that does not exist (not a regular file). "
                "Remove the link, then try again."
            )
            report.unreadable.append(ScaffoldProblem(item, text, False))
        else:
            report.missing.append(
                ScaffoldProblem(item, f"{item.path}: missing. {item.fix}", item.action != REQUIRED)
            )
    return report


def _write_template(project_root: Path, name: str) -> bool:
    """Write the packaged template `name` exactly as `dango init` does; never over a file."""
    from jinja2 import Environment, PackageLoader

    content = Environment(loader=PackageLoader("dango", "templates")).get_template(name).render()
    try:
        with open(project_root / name, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError:
        return False
    if name == "entrypoint.sh":
        (project_root / name).chmod(0o755)
    return True


def restore_scaffold(project_root: Path, problems: list[ScaffoldProblem]) -> list[str]:
    """Recreate the missing `restore` and `auto` items among `problems`; return one line each."""
    done: list[str] = []
    for problem in problems:
        item = problem.item
        if item.path in _TEMPLATE_FILES and item.action == RESTORE:
            if _write_template(project_root, item.path):
                done.append(f"Recreated {item.path}")
        elif item.action == AUTO and not (project_root / item.path).exists():
            if item.path == ".dlt":
                from dango.config.credentials import init_dlt_directory

                init_dlt_directory(project_root)
            else:
                (project_root / item.path).mkdir(parents=True, exist_ok=True)
            done.append(f"Recreated {item.path}")
    return done


def enforce_scaffold(project_root: Path, command: str, *, assume_yes: bool = False) -> None:
    """Make sure the scaffold `command` needs is there, or print what to do and abort.

    Nothing is written when the command has to stop. Empty directories are recreated silently,
    a missing template copy only after `assume_yes` or a yes at the prompt.
    """
    report = check_scaffold(project_root, command)
    blocking = [p for p in (*report.unreadable, *report.missing) if p.item.action == REQUIRED]
    blocking += [p for p in report.unreadable if p.item.action != REQUIRED]
    restorable = [p for p in report.missing if p.item.action == RESTORE]

    if not blocking and restorable:
        from dango.cli.utils import safe_confirm

        names = ", ".join(p.item.path for p in restorable)
        if assume_yes or safe_confirm(
            f"Recreate {names} from Dango's packaged templates?", default=False
        ):
            for line in restore_scaffold(project_root, restorable):
                console.print(f"[dim]{escape(line)}[/dim]")
            restorable = []
    if blocking or restorable:
        console.print(
            f"[red]Error:[/red] this project is missing files that 'dango {command}' needs:"
        )
        for problem in (*blocking, *restorable):
            console.print(f"  - {escape(problem.message)}")
        raise click.Abort()

    for line in restore_scaffold(
        project_root, [p for p in report.missing if p.item.action == AUTO]
    ):
        console.print(f"[dim]{escape(line)}[/dim]")
