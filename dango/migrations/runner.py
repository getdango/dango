"""dango/migrations/runner.py

Core migration engine for applying schema migrations to SQLite databases.

Each database (auth, scheduler, etc.) has its own migration subdirectory
under ``dango/migrations/`` with independently versioned migration files.
The runner discovers, validates, and applies pending migrations within
transactions, recording each applied version in a ``_migrations`` table.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from dango.exceptions import MigrationApplicationError, MigrationDiscoveryError

# Pattern: NNN_description.py (e.g., 001_initial_auth.py)
_MIGRATION_FILE_RE = re.compile(r"^(\d{3,})_.+\.py$")


@dataclass(frozen=True)
class MigrationInfo:
    """A discovered migration file ready to apply."""

    version: int
    description: str
    file_path: Path


@dataclass(frozen=True)
class AppliedMigration:
    """A migration that has already been applied."""

    version: int
    description: str
    applied_at: str


@dataclass(frozen=True)
class MigrationStatus:
    """Full status of a single database's migrations."""

    db_name: str
    db_path: Path
    current_version: int
    applied: list[AppliedMigration] = field(default_factory=list)
    pending: list[MigrationInfo] = field(default_factory=list)


def _load_migration_module(file_path: Path) -> ModuleType:
    """Load a migration file as a Python module."""
    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load migration file: {file_path}"
        raise MigrationDiscoveryError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class MigrationRunner:
    """Discovers and applies migrations for a single database.

    Args:
        db_path: Path to the SQLite database file.
        db_name: Logical name (e.g. ``"auth"``).
        migrations_dir: Directory containing ``NNN_*.py`` migration files.
    """

    def __init__(self, db_path: Path, db_name: str, migrations_dir: Path) -> None:
        self.db_path = db_path
        self.db_name = db_name
        self.migrations_dir = migrations_dir
        self._discovered_cache: list[MigrationInfo] | None = None

    def _connect(self) -> sqlite3.Connection:
        """Open a connection, creating the database file if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.db_path))

    def ensure_migrations_table(self, conn: sqlite3.Connection) -> None:
        """Create the ``_migrations`` tracking table if it does not exist."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            )
            """
        )

    def get_applied_versions(self) -> list[int]:
        """Return sorted list of already-applied migration versions."""
        conn = self._connect()
        try:
            self.ensure_migrations_table(conn)
            rows = conn.execute("SELECT version FROM _migrations ORDER BY version").fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

    def discover_migrations(self) -> list[MigrationInfo]:
        """Scan the migrations directory for valid migration files.

        Results are cached for the lifetime of this runner instance to avoid
        redundant filesystem scanning across ``get_pending()`` and ``status()``.

        Returns:
            Sorted list of ``MigrationInfo`` objects.

        Raises:
            MigrationDiscoveryError: On duplicate versions, malformed files,
                or syntax errors in migration modules.
        """
        if self._discovered_cache is not None:
            return self._discovered_cache

        if not self.migrations_dir.exists():
            self._discovered_cache = []
            return self._discovered_cache

        migrations: list[MigrationInfo] = []
        seen_versions: dict[int, Path] = {}

        for path in sorted(self.migrations_dir.iterdir()):
            if not path.is_file() or path.suffix != ".py":
                continue
            if path.name == "__init__.py":
                continue

            match = _MIGRATION_FILE_RE.match(path.name)
            if not match:
                continue

            filename_version = int(match.group(1))

            try:
                module = _load_migration_module(path)
            except SyntaxError as exc:
                msg = f"Syntax error in migration {path.name}: {exc}"
                raise MigrationDiscoveryError(msg) from exc

            # Validate required attributes
            for attr in ("VERSION", "DESCRIPTION", "upgrade"):
                if not hasattr(module, attr):
                    msg = f"Migration {path.name} is missing required attribute '{attr}'"
                    raise MigrationDiscoveryError(msg)

            version = module.VERSION
            if not isinstance(version, int):
                msg = f"Migration {path.name}: VERSION must be an int, got {type(version).__name__}"
                raise MigrationDiscoveryError(msg)

            if version != filename_version:
                msg = (
                    f"Migration {path.name}: VERSION={version} does not match "
                    f"filename prefix {filename_version}"
                )
                raise MigrationDiscoveryError(msg)

            if not isinstance(module.DESCRIPTION, str):
                msg = f"Migration {path.name}: DESCRIPTION must be a str"
                raise MigrationDiscoveryError(msg)

            if not callable(module.upgrade):
                msg = f"Migration {path.name}: upgrade must be callable"
                raise MigrationDiscoveryError(msg)

            if version in seen_versions:
                msg = (
                    f"Duplicate migration version {version}: "
                    f"{seen_versions[version].name} and {path.name}"
                )
                raise MigrationDiscoveryError(msg)

            seen_versions[version] = path
            migrations.append(
                MigrationInfo(
                    version=version,
                    description=module.DESCRIPTION,
                    file_path=path,
                )
            )

        self._discovered_cache = sorted(migrations, key=lambda m: m.version)
        return self._discovered_cache

    def get_pending(self) -> list[MigrationInfo]:
        """Return migrations that have not yet been applied."""
        applied = set(self.get_applied_versions())
        return [m for m in self.discover_migrations() if m.version not in applied]

    def apply_one(self, migration: MigrationInfo) -> None:
        """Apply a single migration inside a transaction.

        Raises:
            MigrationApplicationError: If the migration's ``upgrade()`` fails.
        """
        conn = self._connect()
        try:
            self.ensure_migrations_table(conn)
            conn.execute("BEGIN")
            try:
                module = _load_migration_module(migration.file_path)
                module.upgrade(conn)

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO _migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.description, now),
                )
                conn.commit()
                self._discovered_cache = None  # invalidate after successful mutation
            except Exception as exc:
                conn.rollback()
                msg = (
                    f"Migration {migration.version} ({migration.description}) "
                    f"failed for {self.db_name}: {exc}"
                )
                raise MigrationApplicationError(
                    msg,
                    context={
                        "db_name": self.db_name,
                        "version": migration.version,
                        "description": migration.description,
                    },
                ) from exc
        finally:
            conn.close()

    def apply_pending(self) -> list[MigrationInfo]:
        """Apply all pending migrations in version order.

        Returns:
            List of migrations that were applied.

        Raises:
            MigrationApplicationError: On failure (subsequent migrations are skipped).
        """
        pending = self.get_pending()
        applied: list[MigrationInfo] = []
        for migration in pending:
            self.apply_one(migration)
            applied.append(migration)
        return applied

    def current_version(self) -> int:
        """Return the highest applied migration version, or 0 if none."""
        versions = self.get_applied_versions()
        return versions[-1] if versions else 0

    def status(self) -> MigrationStatus:
        """Return full migration status for this database.

        Uses a single connection to fetch applied migrations, then computes
        pending and current_version from cached discovery + that data.
        """
        conn = self._connect()
        try:
            self.ensure_migrations_table(conn)
            rows = conn.execute(
                "SELECT version, description, applied_at FROM _migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

        applied: list[AppliedMigration] = []
        applied_versions: set[int] = set()
        current = 0
        for r in rows:
            applied.append(AppliedMigration(version=r[0], description=r[1], applied_at=r[2]))
            applied_versions.add(r[0])
            current = r[0]  # rows are ORDER BY version, so last wins
        pending = [m for m in self.discover_migrations() if m.version not in applied_versions]

        return MigrationStatus(
            db_name=self.db_name,
            db_path=self.db_path,
            current_version=current,
            applied=applied,
            pending=pending,
        )
