"""dango/utils/refresh_backup.py

Name of the table a file-source full refresh keeps its previous data in, and the test for it.

A full refresh of a csv / local_files source renames the live table to ``<name>_pre_refresh_backup``
while it reloads. That table is a safety copy, never a source table: code that lists raw tables to
create models, tests or monitors leaves it out. Level 0 module (standard library only).
"""

from __future__ import annotations

REFRESH_BACKUP_SUFFIX = "_pre_refresh_backup"


def refresh_backup_table_name(source_name: str) -> str:
    """Name of the backup table for *source_name*'s full refresh."""
    return f"{source_name}{REFRESH_BACKUP_SUFFIX}"


def is_refresh_backup_table(table_name: str) -> bool:
    """True when *table_name* is a full-refresh backup table (case-insensitive)."""
    return table_name.lower().endswith(REFRESH_BACKUP_SUFFIX)
