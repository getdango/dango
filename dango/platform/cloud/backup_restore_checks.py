"""dango/platform/cloud/backup_restore_checks.py

What a restore verifies in an extracted backup before it changes anything, and the line format
that carries its warnings back to the client.

The manifest inside an archive lists the files the backup copied; a restore refuses an archive
that lacks one of the fixed-name files it lists (damaged or hand-made) instead of reporting
success and keeping the live file.  Names under the backed-up directories are never trusted: a
file name with a newline leaves fragments in the manifest, and a notebook user picks those
names.  A backup that holds Metabase's database is refused when the server has no Metabase
volume to put it in: the alternative was a silent skip and an empty Metabase.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

#: Files a manifest may list that need not be in the archive: H2's trace log is diagnostic only.
OPTIONAL_MANIFEST_FILES = frozenset({"metabase/metabase.db.trace.db"})
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
_SHOWN = 5


class ManifestError(ValueError):
    """``manifest.json`` is not a readable manifest."""


def manifest_files(text: str) -> list[str]:
    """Paths the manifest lists (none when it has no ``files`` list).

    Raises:
        ManifestError: If the text is not a JSON object, or ``files`` is not a list of
            objects with a string ``path``.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError("it is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ManifestError("it is not a JSON object")
    files = data.get("files", [])
    if not isinstance(files, list):
        raise ManifestError("its file list is not a list")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ManifestError("a file entry has no path")
        paths.append(entry["path"])
    return paths


def fixed_manifest_files() -> frozenset[str]:
    """Manifest entries a restore enforces: names Dango itself fixes, never a name a user picks."""
    from dango.platform.cloud.backup import BACKUP_FILES, DATABASE_FILES

    fixed = {*BACKUP_FILES, *DATABASE_FILES, H2_FILES[0]}
    return frozenset(fixed - OPTIONAL_MANIFEST_FILES)


def missing_manifest_files(text: str, exists: Callable[[list[str]], list[str]]) -> list[str]:
    """Fixed-name files the manifest lists that *exists* reports missing.

    *exists* gets the listed fixed names and returns those that are absent from the archive.
    """
    fixed = fixed_manifest_files()
    listed = sorted({p for p in manifest_files(text) if p in fixed})
    return sorted(exists(listed)) if listed else []


def _refusal(missing: list[str]) -> str:
    shown = ", ".join(missing[:_SHOWN])
    more = f", and {len(missing) - _SHOWN} more" if len(missing) > _SHOWN else ""
    return (
        f"Archive is incomplete: manifest.json lists {len(missing)} file(s) that are not in "
        f"the archive ({shown}{more}). Nothing was restored."
    )


def _check(text: str, exists: Callable[[list[str]], list[str]]) -> None:
    try:
        missing = missing_manifest_files(text, exists)
    except ManifestError as exc:
        raise CloudProvisioningError(
            f"Archive manifest.json cannot be used: {exc}. Nothing was restored."
        ) from exc
    if missing:
        raise CloudProvisioningError(_refusal(missing))


def check_staged_manifest(ssh: SSHManager, staging: str) -> None:
    """Refuse (over SSH, as root) an extracted archive that lacks a fixed-name file its manifest lists.

    Raises:
        CloudProvisioningError: On a damaged manifest, a missing file, or a failed check.
    """
    manifest = ssh.exec_command(f"cat {shlex.quote(staging)}/manifest.json", timeout=120)
    if not manifest.success:
        raise CloudProvisioningError(
            "Archive could not be checked against its manifest. Nothing was restored."
        )

    def absent(names: list[str]) -> list[str]:
        """The fixed *names* that are not regular files in the staged archive."""
        tests = "".join(
            f"[ -f {shlex.quote(f'{staging}/{n}')} ] || echo {shlex.quote(n)}; " for n in names
        )
        result = ssh.exec_command(tests + "true", timeout=120)
        if not result.success:
            raise CloudProvisioningError(
                "Archive could not be checked against its manifest. Nothing was restored."
            )
        return [line for line in result.stdout.splitlines() if line in names]

    _check(manifest.stdout, absent)


def check_local_manifest(staging: Path) -> None:
    """Same check for an archive extracted on this machine; no manifest means nothing to compare."""
    manifest = staging / "manifest.json"
    if not manifest.is_file():
        return
    _check(
        manifest.read_text(encoding="utf-8", errors="replace"),
        lambda names: [n for n in names if not (staging / n).is_file()],
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
