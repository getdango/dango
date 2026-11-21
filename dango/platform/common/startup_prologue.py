"""dango/platform/common/startup_prologue.py

The local startup steps that ``dango serve`` and ``dango web`` run before they serve.

Cheap, local steps that guard against data damage: the project ``.env`` file, the DuckDB
version alignment check (before any DuckDB write: write mode migrates the file format
irreversibly), log rotation, pending migrations, and the dbt schemas. Steps that start
containers or need Docker/Metabase or the network stay in the callers.

Runs on Windows, macOS and Linux. Callers decide how a failure is shown: this module only
raises :class:`PrologueError` with the step's label and the original exception.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


class PrologueError(Exception):
    """A prologue step failed; ``label`` names the step and ``cause`` is the original error."""

    def __init__(self, label: str, cause: Exception) -> None:
        super().__init__(f"{label}: {cause}")
        self.label = label
        self.cause = cause


def load_project_env(project_root: Path) -> None:
    """Load ``<project>/.env`` into the process environment (existing variables win).

    Makes ``DANGO_ADMIN_EMAIL`` and other variables available when the process was not
    started from a shell that exported them (systemd, a desktop launcher) (BUG-100).
    """
    from dotenv import load_dotenv

    load_dotenv(project_root / ".env")


def run_local_prologue(
    project_root: Path,
    *,
    after_migrations: Callable[[bool], None] | None = None,
    lock_schemas: bool = False,
) -> dict[str, Any]:
    """Run the shared startup steps in order and return the applied migrations.

    Order: version alignment, log rotation, migrations, stale-lock cleanup,
    ``after_migrations(stale_lock_removed)``, dbt schemas.

    Args:
        project_root: Project root directory.
        after_migrations: Called with whether a stale dbt lock was removed, before the
            schemas are written (``serve`` stops leftover containers here).
        lock_schemas: Hold the dbt lock (single DuckDB writer) while the schemas are
            written. ``serve`` does not: it has always run this step unlocked.

    Raises:
        PrologueError: The version alignment check, the migrations or the schema step failed.
            Any other exception from the alignment check propagates unchanged.
    """
    from dango.exceptions import VersionMismatchError
    from dango.platform.common import startup

    # Version alignment check: must run BEFORE any DuckDB write operations because write
    # mode auto-migrates the file format irreversibly.
    try:
        startup.check_duckdb_version_alignment()
    except VersionMismatchError as exc:
        raise PrologueError("DuckDB version mismatch", exc) from exc

    startup.rotate_logs(project_root)

    try:
        migrations = startup.run_pending_migrations(project_root)
    except Exception as exc:
        raise PrologueError("Migration failed", exc) from exc

    stale_lock_removed = startup.cleanup_stale_dbt_lock(project_root)
    if after_migrations is not None:
        after_migrations(stale_lock_removed)

    try:
        if lock_schemas:
            from dango.utils.dbt_lock import dbt_lock

            with dbt_lock(project_root, source="cli", operation="web startup schemas"):
                startup.ensure_dbt_schemas(project_root)
        else:
            startup.ensure_dbt_schemas(project_root)
    except Exception as exc:
        raise PrologueError("Schema setup failed", exc) from exc

    return migrations
