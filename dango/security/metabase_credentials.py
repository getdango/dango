"""dango/security/metabase_credentials.py

Protected storage for a project's Metabase administrator password. Metabase
metadata remains in ``.dango/metabase.yml``; this module stores only the
password outside the project tree so it is not copied by sync or backup flows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import keyring

from dango.config import ConfigLoader

_SERVICE_NAME = "dango-metabase"
_LOCAL_SECRETS_DIR = Path.home() / ".dango" / "secrets" / "metabase"
_CLOUD_SECRETS_DIR = Path("/srv/dango/secrets/metabase")


class MetabaseCredentialStoreError(RuntimeError):
    """Raised when a Metabase credential cannot be resolved safely."""


class MetabaseCredentialStore:
    """Store one Metabase administrator password for a stable project id.

    A workstation prefers the operating-system keyring.  If that keyring is
    unavailable, the fallback remains outside the project directory.  Cloud
    servers always use their service-owned path and never attempt a keyring.
    """

    def __init__(self, project_root: Path, *, cloud_mode: bool | None = None):
        self.project_root = Path(project_root)
        self.cloud_mode = (
            os.environ.get("DANGO_CLOUD_MODE") == "true" if cloud_mode is None else cloud_mode
        )
        self.project_id = self._load_persisted_project_id()
        self._using_fallback = self.cloud_mode

    @property
    def secret_path(self) -> Path | None:
        """Return the fallback path only after fallback/cloud storage is used."""
        if self.cloud_mode or self._using_fallback:
            return self._fallback_path
        return None

    @property
    def _fallback_path(self) -> Path:
        base_dir = _CLOUD_SECRETS_DIR if self.cloud_mode else _LOCAL_SECRETS_DIR
        return base_dir / f"{self.project_id}.json"

    def save(self, password: str) -> None:
        """Persist ``password`` in the selected protected credential store."""
        if not isinstance(password, str):
            raise TypeError("Metabase administrator password must be a string")

        if not self.cloud_mode:
            try:
                keyring.set_password(_SERVICE_NAME, self.project_id, password)
                self._using_fallback = False
                return
            except Exception:  # noqa: BLE001 - keyring backends have varied exception types
                self._using_fallback = True

        self._write_fallback(password)

    def load(self) -> str | None:
        """Load the password, returning ``None`` when no credential exists."""
        if not self.cloud_mode:
            try:
                password = keyring.get_password(_SERVICE_NAME, self.project_id)
                self._using_fallback = False
                return password or None
            except Exception:  # noqa: BLE001 - use fallback only when keyring is unavailable
                self._using_fallback = True

        return self._load_fallback()

    def delete(self) -> None:
        """Delete the stored password if present."""
        if not self.cloud_mode:
            try:
                keyring.delete_password(_SERVICE_NAME, self.project_id)
                self._using_fallback = False
                return
            except Exception:  # noqa: BLE001 - absent and unavailable keyrings both use fallback
                self._using_fallback = True

        try:
            self._fallback_path.unlink()
        except FileNotFoundError:
            pass

    def _load_persisted_project_id(self) -> str:
        """Read, but never synthesize, the stable identifier from project.yml."""
        loader = ConfigLoader(self.project_root)
        project = loader.load_project_context()
        project_data = loader.load_yaml(loader.project_file).get("project", {})
        persisted_id = project_data.get("id") if isinstance(project_data, dict) else None
        if not isinstance(persisted_id, str) or not persisted_id:
            raise MetabaseCredentialStoreError(
                "Metabase credential storage requires a persisted project id in .dango/project.yml. "
                "Run the Dango project-identity migration before retrying."
            )
        if project.id != persisted_id:
            raise MetabaseCredentialStoreError(
                "The persisted project id could not be validated for Metabase credential storage."
            )
        return project.id

    def _ensure_fallback_directory(self) -> Path:
        directory = self._fallback_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is affected by umask and existing directories retain their
        # prior mode, so explicitly enforce the credential-directory contract.
        for path in self._credential_directories(directory):
            os.chmod(path, 0o700)
        return directory

    def _credential_directories(self, directory: Path) -> list[Path]:
        # Limit permission changes to the dedicated secret tree.  In cloud
        # mode the tree is /srv/dango/secrets/, not its /srv/dango/ parent.
        root = _CLOUD_SECRETS_DIR.parent if self.cloud_mode else _LOCAL_SECRETS_DIR.parents[1]

        paths: list[Path] = []
        current = directory
        while current != root.parent:
            paths.append(current)
            if current == root:
                break
            current = current.parent
        return list(reversed(paths))

    def _write_fallback(self, password: str) -> None:
        directory = self._ensure_fallback_directory()
        target = self._fallback_path
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        payload = json.dumps({"password": password})

        fd: int | None = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as secret_file:
                fd = None
                secret_file.write(payload)
                secret_file.flush()
                os.fsync(secret_file.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def _load_fallback(self) -> str | None:
        path = self._fallback_path
        if not path.exists():
            return None

        try:
            with path.open(encoding="utf-8") as secret_file:
                payload = json.load(secret_file)
        except json.JSONDecodeError as exc:
            raise MetabaseCredentialStoreError(
                f"Metabase credential fallback file is malformed: {path}"
            ) from exc

        if (
            not isinstance(payload, dict)
            or set(payload) != {"password"}
            or not isinstance(payload["password"], str)
        ):
            raise MetabaseCredentialStoreError(
                f"Metabase credential fallback file is malformed: {path}"
            )
        return payload["password"]
