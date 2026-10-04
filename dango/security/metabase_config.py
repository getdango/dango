"""dango/security/metabase_config.py

Metabase metadata and administrator-credential access boundary.

The project-local ``.dango/metabase.yml`` file is metadata only for new
writes. During the staged 1.0.10 migration, reads retain a narrowly scoped
legacy fallback for existing ``admin.password`` values when the protected
credential store does not yet contain a password.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from dango.security.metabase_credentials import MetabaseCredentialStore


class MetabaseConfigurationError(RuntimeError):
    """Raised when Metabase metadata cannot be read or written safely."""


def load_metabase_metadata(project_root: Path) -> dict[str, Any] | None:
    """Load project-local Metabase metadata without resolving credentials."""
    path = Path(project_root) / ".dango" / "metabase.yml"
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as metadata_file:
            metadata = yaml.safe_load(metadata_file)
    except yaml.YAMLError as exc:
        raise MetabaseConfigurationError("Metabase metadata is malformed.") from exc

    if not isinstance(metadata, dict):
        raise MetabaseConfigurationError("Metabase metadata must be a mapping.")
    return metadata


def load_metabase_admin_credentials(project_root: Path) -> tuple[str, str] | None:
    """Return the administrator email and protected password when available.

    A legacy YAML password is deliberately read only when the protected store
    is empty, which keeps existing projects operable until lifecycle migration
    rotates the administrator password and removes the legacy field.
    """
    metadata = load_metabase_metadata(project_root)
    if metadata is None:
        return None

    admin = metadata.get("admin")
    if not isinstance(admin, dict):
        return None
    email = admin.get("email")
    if not isinstance(email, str) or not email:
        return None

    project_file = Path(project_root) / ".dango" / "project.yml"
    project_id: object = None
    if project_file.exists():
        with project_file.open(encoding="utf-8") as config_file:
            project_config = yaml.safe_load(config_file)
        project = project_config.get("project") if isinstance(project_config, dict) else None
        project_id = project.get("id") if isinstance(project, dict) else None

    if isinstance(project_id, str) and project_id:
        # Do not catch store errors here: a malformed protected credential must
        # be repaired explicitly rather than falling back to plaintext YAML.
        protected_password = MetabaseCredentialStore(project_root).load()
    else:
        # Pre-1.0.8 projects may have no project.yml or no persisted id yet.
        # Their legacy credential remains readable until lifecycle migration
        # establishes the project identity.
        protected_password = None
    if isinstance(protected_password, str) and protected_password:
        return email, protected_password

    legacy_password = admin.get("password")
    if isinstance(legacy_password, str) and legacy_password:
        return email, legacy_password
    return None


def write_metabase_metadata(project_root: Path, metadata: dict[str, Any]) -> None:
    """Atomically write non-secret Metabase metadata with mode ``0600``."""
    admin = metadata.get("admin")
    if isinstance(admin, dict) and "password" in admin:
        raise MetabaseConfigurationError("Metabase metadata must not contain an admin password.")

    path = Path(project_root) / ".dango" / "metabase.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as metadata_file:
            fd = None
            yaml.safe_dump(metadata, metadata_file, default_flow_style=False, sort_keys=False)
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
