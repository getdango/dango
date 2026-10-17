"""tests/factories/backup_create.py

Helpers that run the backup creators (server SSH path and on-server scheduled path) against a local project tree.

``run_server_archive`` executes ``backup._create_archive``'s generated shell for real via
:class:`~tests.factories.backup_shell.ShellSSH`; ``run_scheduled_archive`` runs
``scheduled_backup._create_local_archive`` with the service stop/start stubbed.  Both return
the archive path, the archive's member map (path below the top-level directory -> TarInfo)
and the warnings the creator reported.
"""

from __future__ import annotations

import secrets
import tarfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from tests.factories.backup_shell import ShellSSH

_B = "dango.platform.cloud.backup"
_SB = "dango.platform.cloud.scheduled_backup"


def archive_members(archive: Path) -> dict[str, tarfile.TarInfo]:
    """Map each member's path below the archive's top-level directory to its TarInfo."""
    with tarfile.open(archive, mode="r:gz") as tf:
        return {m.name.split("/", 1)[1]: m for m in tf.getmembers() if "/" in m.name}


def archive_text(archive: Path, rel: str) -> bytes:
    """Return the content of member *rel* (below the top-level directory)."""
    with tarfile.open(archive, mode="r:gz") as tf:
        for member in tf.getmembers():
            if "/" in member.name and member.name.split("/", 1)[1] == rel:
                handle = tf.extractfile(member)
                assert handle is not None
                return handle.read()
    raise KeyError(rel)


def unique_timestamp() -> str:
    """A backup timestamp no other test (or real backup) uses."""
    return f"20260101-{secrets.token_hex(3)}"


def run_server_archive(
    tmp_path: Path,
    project: Path,
    *,
    include_secrets: bool = False,
    staging_root: Path | None = None,
    timestamp: str | None = None,
    ssh: ShellSSH | None = None,
) -> tuple[Path, dict[str, tarfile.TarInfo], list[str], ShellSSH]:
    """Run ``_create_archive`` (no Metabase volume) for *project*; return archive, members, warnings, ssh."""
    from dango.platform.cloud import backup

    backups = tmp_path / "backups" / "deploy"
    staging_root = staging_root or tmp_path / "staging-root"
    staging_root.mkdir(parents=True, exist_ok=True)
    ssh = ssh or ShellSSH(tmp_path / "shim")
    warnings: list[str] = []
    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.BACKUP_DIR", str(backups)),
        patch(f"{_B}.BACKUP_STAGING_ROOT", str(staging_root), create=True),
    ):
        archive_path, _manifest = backup._create_archive(
            ssh,
            timestamp or unique_timestamp(),
            None,
            "test",
            include_secrets=include_secrets,
            warnings=warnings,
        )
    archive = Path(archive_path)
    return archive, archive_members(archive), warnings, ssh


def run_scheduled_archive(
    tmp_path: Path,
    project: Path,
    *,
    include_secrets: bool = False,
    checkpoint: Callable[[], list[str]] | None = None,
) -> tuple[Path, dict[str, tarfile.TarInfo], list[str]]:
    """Run ``_create_local_archive`` for *project*; return archive, members, warnings.

    The real database checkpoint runs unless *checkpoint* replaces it.
    """
    from dango.platform.cloud import scheduled_backup

    backups = tmp_path / "sched-backups" / "deploy"
    extra = [patch(f"{_SB}._checkpoint_databases", side_effect=checkpoint)] if checkpoint else []
    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", backups),
        patch(f"{_SB}._stop_services"),
        patch(f"{_SB}._start_services"),
        patch(f"{_SB}._get_metabase_volume_path", return_value=None),
    ):
        for p in extra:
            p.start()
        try:
            archive, _manifest, warnings = scheduled_backup._create_local_archive(
                "test", include_secrets=include_secrets
            )
        finally:
            for p in extra:
                p.stop()
    return archive, archive_members(archive), warnings
