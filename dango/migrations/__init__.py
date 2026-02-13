"""dango/migrations/__init__.py

Database migration framework for Dango.

Provides incremental schema migrations for SQLite databases used by
subsystems (auth, scheduler, etc.). Each subsystem has its own
subdirectory under ``dango/migrations/`` with independently versioned
migration files.

Public API::

    from dango.migrations import apply_all_pending, get_all_status
"""

from __future__ import annotations

from pathlib import Path

from .runner import (
    AppliedMigration,
    MigrationInfo,
    MigrationRunner,
    MigrationStatus,
)

__all__ = [
    "apply_all_pending",
    "get_all_status",
    "AppliedMigration",
    "MigrationInfo",
    "MigrationRunner",
    "MigrationStatus",
]


def _get_migrations_base_dir() -> Path:
    """Return the directory containing migration subdirectories."""
    return Path(__file__).parent


def apply_all_pending(project_root: Path) -> dict[str, list[MigrationInfo]]:
    """Discover all migration subdirectories and apply pending migrations.

    Convention: database ``'auth'`` maps to ``<project_root>/.dango/auth.db``.
    Subdirectories without migration files are skipped.

    Args:
        project_root: Root of the Dango project.

    Returns:
        Mapping of database name to list of applied migrations.
    """
    base_dir = _get_migrations_base_dir()
    results: dict[str, list[MigrationInfo]] = {}

    for subdir in sorted(base_dir.iterdir()):
        if not subdir.is_dir():
            continue
        if subdir.name.startswith("__"):
            continue

        # Check if the subdirectory has any Python migration files
        py_files = [
            f
            for f in subdir.iterdir()
            if f.is_file() and f.suffix == ".py" and f.name != "__init__.py"
        ]
        if not py_files:
            continue

        db_path = project_root / ".dango" / f"{subdir.name}.db"
        runner = MigrationRunner(db_path=db_path, db_name=subdir.name, migrations_dir=subdir)
        applied = runner.apply_pending()
        results[subdir.name] = applied

    return results


def get_all_status(project_root: Path) -> list[MigrationStatus]:
    """Get migration status for all known databases.

    Args:
        project_root: Root of the Dango project.

    Returns:
        List of ``MigrationStatus`` objects, one per database.
    """
    base_dir = _get_migrations_base_dir()
    statuses: list[MigrationStatus] = []

    for subdir in sorted(base_dir.iterdir()):
        if not subdir.is_dir():
            continue
        if subdir.name.startswith("__"):
            continue

        # Check if the subdirectory has any Python migration files
        py_files = [
            f
            for f in subdir.iterdir()
            if f.is_file() and f.suffix == ".py" and f.name != "__init__.py"
        ]
        if not py_files:
            continue

        db_path = project_root / ".dango" / f"{subdir.name}.db"
        runner = MigrationRunner(db_path=db_path, db_name=subdir.name, migrations_dir=subdir)
        statuses.append(runner.status())

    return statuses
