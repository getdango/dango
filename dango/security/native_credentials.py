"""dango/security/native_credentials.py

Store native credential bundles outside projects; references confer no caller authorization.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import keyring
import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_SERVICE_NAME = "dango-native"
_LOCAL_SECRETS_DIR = Path.home() / ".dango/secrets/native"
_CLOUD_SECRETS_DIR = Path("/srv/dango/secrets/native")
_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_LIMIT = 1024 * 1024


def _identifier(value: str) -> str:
    if (
        not value.strip()
        or any(c in value for c in ("/", "\\", ".."))
        or any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Supply a nonblank opaque identifier without traversal or controls.")
    return value


_Identifier = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_identifier)]


class _Model(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class SecretRef(_Model):
    """Identify protected storage only; the trusted server must authorize its current actor."""

    project_id: _Identifier
    actor_id: _Identifier
    credential_id: _Identifier
    version: _Identifier


class _Record(_Model):
    ref: SecretRef
    backend: Literal["keyring", "file"]
    state: Literal["active", "revoked"]


class NativeCredentialStoreError(RuntimeError):
    """Expose fixed safe diagnostics without payloads, paths or backend exceptions."""

    def __init__(self) -> None:
        self.code = "native_credential_unavailable"
        self.action = (
            "Ask an administrator to verify project identity and protected native storage."
        )
        super().__init__(f"{self.code}: {self.action}")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _credential(value: object) -> dict[str, str]:
    if (
        type(value) is not dict
        or not value
        or any(type(k) is not str or not k or type(v) is not str or not v for k, v in value.items())
    ):
        raise NativeCredentialStoreError()
    payload = json.dumps(value, ensure_ascii=True)
    if len(payload) > _LIMIT:
        raise NativeCredentialStoreError()
    return dict(value)


def _owned(fd: int, mode: int, *, directory: bool = False) -> None:
    info = os.fstat(fd)
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or (not directory and info.st_nlink != 1):
        raise NativeCredentialStoreError()
    if stat.S_IMODE(info.st_mode) != mode:
        raise NativeCredentialStoreError()


def _directory(parent: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        # The no-follow open and descriptor checks below validate existing entries.
        name = str(name)
    fd = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=parent)
    try:
        _owned(fd, 0o700, directory=True)
    except Exception:
        os.close(fd)
        raise
    return fd


def _read(directory: int, name: str) -> str | None:
    try:
        fd = os.open(name, _FLAGS | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        _owned(stream.fileno(), 0o600)
        value = stream.read(_LIMIT + 1)
        if len(value) > _LIMIT:
            raise NativeCredentialStoreError()
        return value


def _write(directory: int, name: str, payload: str) -> None:
    temporary = f".{uuid4().hex}.pending"
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Refuse an existing symlink/unsafe destination instead of replacing it.
        _read(directory, name)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        _unlink(directory, temporary)


def _unlink(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return


class NativeCredentialStore:
    """Maintain one active native credential bundle per actor, outside the project.

    A fresh save supersedes the prior reference only after candidate validation and
    metadata promotion. Versions are opaque labels, not ordered revision counters.
    File fallback is owner-only plaintext, not a key-managed disaster recovery system.
    """

    def __init__(self, project_root: Path, *, cloud_mode: bool | None = None):
        try:
            if cloud_mode is not None and type(cloud_mode) is not bool:
                raise NativeCredentialStoreError()
            self.project_root = Path(project_root)
            self.cloud_mode = (
                os.environ.get("DANGO_CLOUD_MODE") == "true" if cloud_mode is None else cloud_mode
            )
            self.project_id = self._project_id()
        except Exception:
            raise NativeCredentialStoreError() from None

    def _project_id(self) -> str:
        root = os.open(self.project_root, _FLAGS | os.O_DIRECTORY)
        try:
            metadata = os.open(".dango", _FLAGS | os.O_DIRECTORY, dir_fd=root)
            try:
                fd = os.open("project.yml", _FLAGS | os.O_NONBLOCK, dir_fd=metadata)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise NativeCredentialStoreError()
                    data = yaml.safe_load(stream.read(_LIMIT))
            finally:
                os.close(metadata)
        finally:
            os.close(root)
        project_id = data["project"]["id"]
        if type(project_id) is not str:
            raise NativeCredentialStoreError()
        return _identifier(project_id)

    def _ref(self, ref: SecretRef) -> SecretRef:
        if type(ref) is not SecretRef:
            raise NativeCredentialStoreError()
        ref = SecretRef.model_validate(ref.model_dump(warnings=False))
        if ref.project_id != self.project_id:
            raise NativeCredentialStoreError()
        return ref

    @contextmanager
    def _scope(self, ref: SecretRef) -> Iterator[int]:
        if self._project_id() != self.project_id:
            raise NativeCredentialStoreError()
        root = _CLOUD_SECRETS_DIR if self.cloud_mode else _LOCAL_SECRETS_DIR
        if (
            not root.is_absolute()
            or ".." in root.parts
            or root.resolve().is_relative_to(self.project_root.resolve())
        ):
            raise NativeCredentialStoreError()
        fd = os.open("/", _FLAGS | os.O_DIRECTORY)
        lock: int | None = None
        try:
            # Walk every component without following symlinks. Only the dedicated
            # secret subtree is required to be private; OS/home parents retain modes.
            for index, part in enumerate(root.parts[1:], start=1):
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    part = str(part)
                child = os.open(part, _FLAGS | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                if index >= len(root.parts) - (2 if self.cloud_mode else 3):
                    _owned(fd, 0o700, directory=True)
            _owned(fd, 0o700, directory=True)
            for component in (_digest(ref.project_id), _digest(ref.actor_id)):
                child = _directory(fd, component)
                os.close(fd)
                fd = child
            try:
                lock = os.open(
                    "lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
                )
            except FileExistsError:
                lock = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            _owned(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield fd
        finally:
            if lock is not None:
                os.close(lock)
            os.close(fd)

    def _record(self, directory: int) -> _Record | None:
        text = _read(directory, "record.json")
        record = None if text is None else _Record.model_validate(json.loads(text))
        if self.cloud_mode and record is not None and record.backend != "file":
            raise NativeCredentialStoreError()
        return record

    def _matching(self, directory: int, ref: SecretRef) -> _Record | None:
        record = self._record(directory)
        if record is None or record.state != "active" or record.ref != ref:
            return None
        return record

    def _key(self, ref: SecretRef) -> str:
        return _digest(ref.model_dump(mode="json"))

    def _load(self, directory: int, record: _Record) -> dict[str, str] | None:
        key = self._key(record.ref)
        if record.backend == "keyring":
            text = keyring.get_password(_SERVICE_NAME, key)
        else:
            text = _read(directory, f"{key}.json")
        return None if text is None else _credential(json.loads(text))

    def _delete(self, directory: int, record: _Record) -> None:
        key = self._key(record.ref)
        if record.backend == "keyring":
            keyring.delete_password(_SERVICE_NAME, key)
        else:
            _unlink(directory, f"{key}.json")

    def _candidate(self, directory: int, ref: SecretRef, payload: str) -> _Record:
        key = self._key(ref)
        backend: Literal["keyring", "file"] = "file"
        if not self.cloud_mode:
            try:
                keyring.set_password(_SERVICE_NAME, f"{key}:pending", payload)
                backend = "keyring"
            except Exception:
                backend = "file"
        record = _Record(ref=ref, backend=backend, state="active")
        if backend == "keyring":
            try:
                candidate = keyring.get_password(_SERVICE_NAME, f"{key}:pending")
                if candidate != payload:
                    raise NativeCredentialStoreError()
                _credential(json.loads(candidate))
                keyring.set_password(_SERVICE_NAME, key, candidate)
                if self._load(directory, record) != json.loads(payload):
                    raise NativeCredentialStoreError()
            finally:
                self._discard_pending(key)
        else:
            pending = f"{key}.pending.json"
            try:
                _write(directory, pending, payload)
                candidate = _read(directory, pending)
                if candidate != payload:
                    raise NativeCredentialStoreError()
                _credential(json.loads(candidate))
                _write(directory, f"{key}.json", candidate)
            finally:
                _unlink(directory, pending)
        return record

    def _discard_pending(self, key: str) -> bool:
        try:
            keyring.delete_password(_SERVICE_NAME, f"{key}:pending")
            return True
        except Exception:
            return False

    def _cleanup(self, directory: int, record: _Record | None) -> bool:
        try:
            if record is not None:
                self._delete(directory, record)
            return True
        except Exception:
            # Unreferenced residue cannot resolve through active metadata.
            return False

    def save(self, actor_id: str, credential: dict[str, str], version: str) -> SecretRef:
        """Validate and promote a fresh bundle, preserving active state until promotion."""
        try:
            value = _credential(credential)
            ref = SecretRef(
                project_id=self.project_id,
                actor_id=actor_id,
                credential_id=uuid4().hex,
                version=version,
            )
            with self._scope(ref) as directory:
                previous = self._record(directory)
                if previous is not None and (
                    previous.ref.project_id != ref.project_id or previous.ref.actor_id != actor_id
                ):
                    raise NativeCredentialStoreError()
                candidate = self._candidate(directory, ref, json.dumps(value, ensure_ascii=True))
                _write(directory, "record.json", candidate.model_dump_json())
                self._cleanup(directory, previous)
            return ref
        except Exception:
            raise NativeCredentialStoreError() from None

    def load(self, ref: SecretRef) -> dict[str, str] | None:
        """Resolve only the current persisted matching reference, without authenticating callers."""
        try:
            ref = self._ref(ref)
            with self._scope(ref) as directory:
                record = self._matching(directory, ref)
                return None if record is None else self._load(directory, record)
        except Exception:
            raise NativeCredentialStoreError() from None

    def revoke(self, ref: SecretRef) -> None:
        """Commit denial before deletion; stale references cannot revoke newer credentials."""
        try:
            ref = self._ref(ref)
            with self._scope(ref) as directory:
                record = self._matching(directory, ref)
                if record is None:
                    return
                revoked = _Record(ref=ref, backend=record.backend, state="revoked")
                _write(directory, "record.json", revoked.model_dump_json())
                self._delete(directory, record)
        except Exception:
            raise NativeCredentialStoreError() from None
