"""dango/security/legacy_backup_artifacts.py

Safe discovery of local artifacts that may predate protected backups.
"""

from __future__ import annotations

from pathlib import Path


def find_legacy_backup_artifacts(project_root: Path) -> list[Path]:
    """Return local backup artifact paths without reading their contents.

    The paths are only candidates: callers must describe them as potentially
    legacy and must not infer that any particular archive contains a secret.
    """
    root = Path(project_root)
    candidates: list[Path] = []
    local_backup_dir = root / ".dango" / "backups"
    if local_backup_dir.exists():
        candidates.append(local_backup_dir)
    candidates.extend(sorted(root.glob("dango-backup-*")))
    return candidates


def legacy_backup_artifact_warning(project_root: Path) -> str | None:
    """Return non-secret guidance for local artifacts that may predate 1.0.10."""
    artifacts = find_legacy_backup_artifacts(project_root)
    if not artifacts:
        return None
    rendered = ", ".join(str(path) for path in artifacts)
    return (
        f"Found {len(artifacts)} local backup artifact(s) that may predate protected "
        f"Metabase metadata: {rendered}. Restrict access or remove them manually; "
        "Dango did not inspect or delete them."
    )
