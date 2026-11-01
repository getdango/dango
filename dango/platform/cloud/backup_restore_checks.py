"""dango/platform/cloud/backup_restore_checks.py

What a restore verifies in an extracted backup before it changes anything, and the line format
that carries its warnings and its refusal back to the client.

A backup that holds Metabase's database is refused when the server has no Metabase volume to put
it in: the alternative was a silent skip and an empty Metabase.  The archive's ``manifest.json``
is deliberately not used to decide anything: an SSH-made manifest is built from newline-split
listings, so a notebook user can put any fixed name into it.
"""

from __future__ import annotations

import shlex
from pathlib import Path

#: Metabase's database files inside an archive (relative to its top-level directory).
H2_FILES = ("metabase/metabase.db.mv.db", "metabase/metabase.db.trace.db")
#: Start of each line a server-side restore prints for a warning (the client shows only these).
RESTORE_WARNING_PREFIX = "DANGO-RESTORE-WARNING "
#: Start of the one line a server-side restore prints when it refuses or fails.
RESTORE_ERROR_PREFIX = "DANGO-RESTORE-ERROR "
METABASE_VOLUME_MISSING = (
    "The Metabase Docker volume does not exist on this server, so the Metabase data in this "
    "backup cannot be restored and Metabase would start empty. Nothing was restored. Restart "
    "dango-web on the server (systemctl restart dango-web) so Dango recreates the volume, "
    "then run the restore again."
)


def spaces_restore_script(key: str) -> str:
    """Python source the client runs on the server for a Spaces restore.

    Prints each warning as one ``RESTORE_WARNING_PREFIX`` line; a failure prints one
    ``RESTORE_ERROR_PREFIX`` line (the message on a single line) and exits 1, so the client shows
    the cause instead of a traceback.  *key* has been checked by the caller.
    """
    return (
        "import sys\n"
        "from dango.platform.cloud.scheduled_backup import "
        "_load_spaces_config, restore_from_spaces\n"
        "try:\n"
        f"    for w in restore_from_spaces(_load_spaces_config(), {key!r}) or []:\n"
        f"        print({RESTORE_WARNING_PREFIX!r} + ' '.join(str(w).split()))\n"
        "except Exception as exc:\n"
        f"    print({RESTORE_ERROR_PREFIX!r} + ' '.join(str(exc).split()))\n"
        "    sys.exit(1)\n"
    )


def staged_h2_command(staging: str) -> str:
    """Shell test that succeeds when the extracted backup holds a Metabase database file."""
    return " || ".join(f"test -f {shlex.quote(f'{staging}/{rel}')}" for rel in H2_FILES)


def has_local_h2(staging: Path) -> bool:
    """True when the backup extracted on this machine holds a Metabase database file."""
    return any((staging / rel).is_file() for rel in H2_FILES)
