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

import os
import re
import tempfile
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

# How long `dango web` waits for another process that holds the dbt lock (it only writes
# idempotent schemas, so it does not wait as long as a sync would).
LOCK_WAIT_TIMEOUT_SECONDS = 60

_SCHEMA_NAMES = frozenset({"raw", "staging", "intermediate", "marts"})


class PrologueError(Exception):
    """A prologue step failed; ``label`` names the step and ``cause`` is the original error."""

    def __init__(self, label: str, cause: Exception) -> None:
        super().__init__(f"{label}: {cause}")
        self.label = label
        self.cause = cause


# Keys `dango start` never takes from the project `.env`: a local run must not be flipped into
# cloud mode, and the project root is always the one the command resolved.
LOCAL_START_RESERVED_ENV = frozenset({"DANGO_CLOUD_MODE", "DANGO_PROJECT_ROOT"})


def load_project_env(project_root: Path, *, skip: Collection[str] = ()) -> tuple[str, ...]:
    """Load ``<project>/.env`` into the process environment (existing variables win).

    Makes ``DANGO_ADMIN_EMAIL`` and other variables available when the process was not
    started from a shell that exported them (systemd, a desktop launcher) (BUG-100), and
    lets every process started from this one inherit them.

    Args:
        project_root: Project root directory.
        skip: Names that must not come from the file. A name already set in the real
            environment is left alone either way.

    Returns:
        The names in ``skip`` that the file defines and that were therefore left unset
        (never values).

    Raises:
        UnicodeDecodeError: The file is not UTF-8; nothing was loaded.
        OSError: The file cannot be read.
    """
    from dotenv import load_dotenv

    before = {name: os.environ.get(name) for name in skip}
    load_dotenv(project_root / ".env", encoding="utf-8")
    ignored = []
    for name, previous in before.items():
        if previous is None and name in os.environ:
            del os.environ[name]
            ignored.append(name)
    return tuple(sorted(ignored))


def describe_lock_holder(lock_info: dict[str, Any] | None) -> str:
    """Plain ASCII description of whoever holds the dbt lock (PID and operation if known)."""
    if not lock_info:
        return "holder unknown"

    def clean(value: object) -> str:
        """Printable ASCII, at most 60 characters."""
        return re.sub(r"[^\x20-\x7e]", "?", str(value))[:60]

    parts = []
    if lock_info.get("pid") is not None:
        parts.append(f"PID {clean(lock_info['pid'])}")
    if lock_info.get("operation"):
        parts.append(clean(lock_info["operation"]))
    return ", ".join(parts) or "holder unknown"


def _schemas_present(project_root: Path) -> bool | None:
    """True when the warehouse already has the four dbt schemas, False when it does not
    (or does not exist), None when another process has the file open and it cannot be read."""
    from dango.utils.duckdb_lock_text import is_duckdb_lock_error

    warehouse = project_root / "data" / "warehouse.duckdb"
    if not warehouse.exists():
        return False
    import duckdb

    try:
        conn = duckdb.connect(str(warehouse), read_only=True)
    except Exception as exc:
        if is_duckdb_lock_error(exc):
            return None
        raise
    try:
        rows = conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
    finally:
        conn.close()
    return _SCHEMA_NAMES <= {row[0] for row in rows}


def _require_writable_lock_folder(project_root: Path) -> None:
    """Fail at once, with the reason, when the dbt lock file cannot be created or opened.

    ``DbtLock`` treats every ``OSError`` from opening its file as "someone else holds it", so
    an unwritable folder would otherwise look like a long wait.
    """
    state = project_root / ".dango" / "state"
    try:
        state.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(dir=state, prefix=".write-check-")
        os.close(handle)
        os.unlink(probe)
        lock_file = state / "dbt.lock"
        if lock_file.exists() and not os.access(lock_file, os.W_OK):
            raise PermissionError(13, "Permission denied", str(lock_file))
    except OSError as exc:
        raise RuntimeError(
            f"cannot write in {state} ({exc.strerror or exc}), so the dbt lock cannot be taken. "
            "Fix the permissions of the .dango folder and run the command again"
        ) from exc


def _ensure_schemas_for_web(
    project_root: Path,
    on_lock_wait: Callable[[str], None] | None,
    on_note: Callable[[str], None] | None,
) -> None:
    """The dbt schemas step of ``dango web``: never stops web for a warehouse that is in use.

    Skips the write when the schemas exist. Otherwise it writes them under the dbt lock: a
    busy lock is announced once and waited for (bounded); a DuckDB lock conflict (another
    process has the file open) is a one-line note, not a failure. Anything else raises.
    """
    from dango.exceptions import DbtLockError
    from dango.platform.common import startup
    from dango.utils.dbt_lock import DbtLock
    from dango.utils.duckdb_lock_text import is_duckdb_lock_error

    busy = (
        "The warehouse is in use by another Dango process; the dbt schemas were not checked "
        "(they only matter for Metabase)."
    )

    def note() -> None:
        """Tell the caller, once, that the warehouse is in use."""
        if on_note is not None:
            on_note(busy)

    present = _schemas_present(project_root)
    if present:
        return
    if present is None:
        note()
        return

    _require_writable_lock_folder(project_root)
    lock = DbtLock(project_root, source="cli", operation="web startup schemas")
    try:
        lock.acquire(timeout=0)
    except DbtLockError as first:
        holder = describe_lock_holder(first.lock_info)
        if on_lock_wait is not None:
            on_lock_wait(holder)
        try:
            lock.acquire(timeout=LOCK_WAIT_TIMEOUT_SECONDS)
        except DbtLockError as second:
            raise RuntimeError(
                f"another Dango process ({describe_lock_holder(second.lock_info)}) still holds "
                f"the warehouse lock after {LOCK_WAIT_TIMEOUT_SECONDS} seconds. "
                "Wait for it to finish, then run the command again"
            ) from second
    try:
        if _schemas_present(project_root):
            return
        try:
            startup.ensure_dbt_schemas(project_root)
        except Exception as exc:
            if not is_duckdb_lock_error(exc):
                raise
            note()
    finally:
        lock.release()


def run_local_prologue(
    project_root: Path,
    *,
    after_migrations: Callable[[bool], None] | None = None,
    lock_schemas: bool = False,
    on_lock_wait: Callable[[str], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the shared startup steps in order and return the applied migrations.

    Order: version alignment, log rotation, migrations, stale-lock cleanup,
    ``after_migrations(stale_lock_removed)``, dbt schemas.

    Args:
        project_root: Project root directory.
        after_migrations: Called with whether a stale dbt lock was removed, before the
            schemas are written (``serve`` stops leftover containers here).
        lock_schemas: ``dango web`` mode for the schemas step: no write when the
            schemas exist, hold the dbt lock (single DuckDB writer) while writing, and never
            fail because another process has the warehouse open. ``serve`` does not use it:
            it has always run this step unchanged and unlocked.
        on_lock_wait: Called once with a description of the holder before waiting for a busy
            dbt lock (``lock_schemas`` mode only).
        on_note: Called with a one-line note when the warehouse is in use and the schemas
            step was left out (``lock_schemas`` mode only).

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
            _ensure_schemas_for_web(project_root, on_lock_wait, on_note)
        else:
            startup.ensure_dbt_schemas(project_root)
    except Exception as exc:
        raise PrologueError("Schema setup failed", exc) from exc

    return migrations
