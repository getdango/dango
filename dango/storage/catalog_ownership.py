"""dango/storage/catalog_ownership.py

Journal private catalog effects and remove only recorded physical objects without symlinks.
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
from typing import Any
from uuid import uuid4

_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_FIELDS = {"binding", "root", "native", "phase", "files"}
_PHASES = {"intent", "root", "namespace", "prepared", "removing", "native_removed", "removed"}


class OwnershipError(RuntimeError):
    """Keep physical ownership failures free of paths, payloads and native diagnostics."""

    def __init__(self) -> None:
        super().__init__("catalog_ownership_unverified")


def _owned(fd: int, *, directory: bool = False, private: bool = True) -> os.stat_result:
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if not kind(info.st_mode) or info.st_uid != os.getuid():
        raise OwnershipError()
    if (private and stat.S_IMODE(info.st_mode) != mode) or (not directory and info.st_nlink != 1):
        raise OwnershipError()
    return info


def directory(path: Path) -> int:
    """Open every ancestor without following links; dedicated roots must be private."""
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise OwnershipError()
    fd = os.open("/", _READ | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        _owned(fd, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def opaque_identifier(value: str) -> str:
    """Validate opaque lookup identifiers before using them in owned record keys."""
    if not value.strip() or any(c in value for c in ("/", "\\", "..")):
        raise ValueError("Invalid identifier.")
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("Invalid identifier.")
    return value


def canonical_path(value: str) -> str:
    """Validate a non-root canonical absolute reference without filesystem activity."""
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or path == Path("/")
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Supply a canonical absolute private path.")
    return value


def _file_exists(root: Path, relative: Path) -> bool:
    fd = directory(root)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, _READ | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            _owned(fd, directory=True, private=False)
        child = os.open(relative.name, _READ, dir_fd=fd)
        try:
            _owned(child, private=False)
        finally:
            os.close(child)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


def catalog_file_paths(rows: list[Any], root: Path) -> set[str]:
    """Bind retained native relative/absolute path triples to one owned root."""
    files = set()
    for sp, sr, tp, tr, fp, fr, end in rows:
        path = root
        for value, relative in ((sp, sr), (tp, tr), (fp, fr)):
            part = Path(value)
            if (
                type(relative) is not bool
                or ".." in part.parts
                or (relative and part.is_absolute())
            ):
                raise OwnershipError()
            path = path / part if relative else part
            if not path.is_absolute() or not path.is_relative_to(root):
                raise OwnershipError()
        exists = _file_exists(root, path.relative_to(root))
        if not exists and end is None:
            raise OwnershipError()
        if exists:
            files.add(str(path.relative_to(root)))
    return files


class OwnedWorkspace:
    """Hold one workspace lock across authorization, native use and durable checkpoints.

    Private roots and all actors using this allocator share this lock. This is not a
    security boundary against another process running as the same operating-system user.
    """

    def __init__(self, state_root: Path, data_root: Path, project: str, workspace: str):
        self.state_root, self.data_root = state_root, data_root
        self.project, self.workspace = project, workspace
        self.key = hashlib.sha256(json.dumps([project, workspace]).encode()).hexdigest()
        self.record: dict[str, Any] | None = None

    @contextmanager
    def locked(self) -> Iterator[OwnedWorkspace]:
        """Resolve private roots, validate the lock and read the authoritative record."""
        self.state = directory(self.state_root)
        try:
            self.data = directory(self.data_root)
            try:
                lock = os.open(
                    self.key + ".lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.state,
                )
                try:
                    _owned(lock)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.record = self._read()
                    yield self
                finally:
                    os.close(lock)
            finally:
                os.close(self.data)
        finally:
            os.close(self.state)

    def _read(self) -> dict[str, Any] | None:
        try:
            fd = os.open(self.key + ".json", _READ, dir_fd=self.state)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            _owned(stream.fileno())
            text = stream.read(1048577)
        if len(text) > 1048576:
            raise OwnershipError()
        record = json.loads(text)
        if type(record) is not dict or set(record) != _FIELDS or record["phase"] not in _PHASES:
            raise OwnershipError()
        if type(record["binding"]) is not dict or type(record["files"]) is not dict:
            raise OwnershipError()
        if (
            record["binding"].get("project_id") != self.project
            or record["binding"].get("workspace_id") != self.workspace
        ):
            raise OwnershipError()
        return record

    def save(self) -> None:
        """Atomically persist redacted intent/evidence before advancing effects."""
        if self.record is None:
            raise OwnershipError()
        payload = json.dumps(self.record, allow_nan=False)
        if len(payload) > 1048576:
            raise OwnershipError()
        temporary = uuid4().hex + ".pending"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.state,
        )
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._read()
            os.replace(temporary, self.key + ".json", src_dir_fd=self.state, dst_dir_fd=self.state)
            os.fsync(self.state)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.state)
            except FileNotFoundError:
                pass

    def begin(self, binding: dict[str, Any]) -> None:
        """Commit intent, then create and record the exact exclusive private root."""
        if self.record is not None:
            raise OwnershipError()
        self.record = {
            "binding": binding,
            "root": None,
            "native": None,
            "phase": "intent",
            "files": {},
        }
        self.save()
        os.mkdir(self.token, 0o700, dir_fd=self.data)
        os.fsync(self.data)
        with self.root(unrecorded=True) as fd:
            if fd is None:
                raise OwnershipError()
            self.record["root"] = _identity(_owned(fd, directory=True))
        self.record["phase"] = "root"
        self.save()

    @property
    def token(self) -> str:
        """Return only the generated hexadecimal root name from a validated binding."""
        if self.record is None:
            raise OwnershipError()
        token = self.record["binding"]["binding_id"]
        if (
            type(token) is not str
            or len(token) != 32
            or any(c not in "0123456789abcdef" for c in token)
        ):
            raise OwnershipError()
        if self.record["binding"]["data_root_ref"] != str(self.data_root / token):
            raise OwnershipError()
        if self.record["binding"].get("metadata_schema") != "dango_" + token:
            raise OwnershipError()
        return token

    @contextmanager
    def root(self, *, absent: bool = False, unrecorded: bool = False) -> Iterator[int | None]:
        """Reopen the exact recorded root without following any component."""
        if self.record is None or (self.record["root"] is None and not unrecorded):
            raise OwnershipError()
        try:
            fd = os.open(self.token, _READ | os.O_DIRECTORY, dir_fd=self.data)
        except FileNotFoundError:
            if not absent:
                raise
            yield None
            return
        try:
            identity = _identity(_owned(fd, directory=True))
            if self.record is None or (
                self.record["root"] is not None and self.record["root"] != identity
            ):
                raise OwnershipError()
            yield fd
        finally:
            os.close(fd)

    def checkpoint(self, native: dict[str, Any], phase: str = "prepared") -> None:
        """Record immutable native identities only after their concrete observation."""
        if self.record is None or phase not in _PHASES:
            raise OwnershipError()
        self.record.update(native=native, phase=phase)
        self.save()

    def validate_root(self) -> None:
        """Refuse any physical symlink, hardlink or special entry before native access."""
        with self.root() as fd:
            if fd is None:
                raise OwnershipError()
            self._tree(fd)

    def recover(self) -> None:
        """Persist an interrupted outcome while retaining all ownership evidence."""
        if self.record is not None:
            self.record["binding"]["state"] = "recovery_required"
            self.record["binding"]["revision"] += 1
            self.save()

    def ready(self) -> dict[str, Any]:
        """Promote only a completed native prepare checkpoint to a ready revision."""
        if self.record is None or self.record["phase"] != "prepared":
            raise OwnershipError()
        value = self.record["binding"]
        value.update(state="ready", revision=value["revision"] + 1)
        self.save()
        return dict(value)

    def _tree(self, fd: int, prefix: str = "") -> dict[str, list[int]]:
        result = {}
        for name in os.listdir(fd):
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=fd)
                try:
                    info = _owned(child, directory=True, private=False)
                    result[relative + "/"] = _identity(info)
                    result.update(self._tree(child, relative + "/"))
                finally:
                    os.close(child)
            else:
                child = os.open(name, _READ, dir_fd=fd)
                try:
                    info = _owned(child, private=False)
                    result[relative] = _identity(info)
                finally:
                    os.close(child)
        return result

    def removal_intent(self, expected_files: set[str]) -> None:
        """Bind the full physical inventory to native metadata before dropping anything."""
        if self.record is None or self.record["root"] is None:
            raise OwnershipError()
        if self.record["phase"] not in {"prepared", "removing"}:
            raise OwnershipError()
        with self.root() as fd:
            if fd is None:
                raise OwnershipError()
            tree = self._tree(fd)
        directories: set[str] = set()
        for name in expected_files:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not name or name.endswith("/"):
                raise OwnershipError()
            directories.update(str(parent) + "/" for parent in path.parents if parent != Path("."))
        if set(tree) != expected_files | directories:
            raise OwnershipError()
        if self.record["phase"] == "removing":
            if tree != self.record["files"]:
                raise OwnershipError()
            return
        self.record.update(files=tree, phase="removing")
        self.record["binding"]["state"] = "recovery_required"
        self.record["binding"]["revision"] += 1
        self.save()

    def native_removed(self) -> None:
        """Commit native absence before beginning physical deletion."""
        if self.record is None or self.record["phase"] not in {"removing", "native_removed"}:
            raise OwnershipError()
        self.record["phase"] = "native_removed"
        self.save()

    def _erase(self, fd: int, prefix: str, expected: dict[str, Any]) -> None:
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            is_directory = stat.S_ISDIR(info.st_mode)
            relative = prefix + name + ("/" if is_directory else "")
            if expected.get(relative) != _identity(info):
                raise OwnershipError()
            if is_directory:
                child = os.open(name, _READ | os.O_DIRECTORY, dir_fd=fd)
                try:
                    if (
                        _identity(_owned(child, directory=True, private=False))
                        != expected[relative]
                    ):
                        raise OwnershipError()
                    self._erase(child, relative, expected)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=fd)
            else:
                child = os.open(name, _READ, dir_fd=fd)
                try:
                    if _identity(_owned(child, private=False)) != expected[relative]:
                        raise OwnershipError()
                finally:
                    os.close(child)
                os.unlink(name, dir_fd=fd)
            os.fsync(fd)

    def finish_removal(self) -> None:
        """Resume recorded deletions, refusing substitutions and newly added objects."""
        if self.record is None or self.record["phase"] != "native_removed":
            raise OwnershipError()
        with self.root(absent=True) as fd:
            if fd is not None:
                tree = self._tree(fd)
                if any(
                    self.record["files"].get(name) != identity for name, identity in tree.items()
                ):
                    raise OwnershipError()
                self._erase(fd, "", self.record["files"])
        try:
            os.rmdir(self.token, dir_fd=self.data)
        except FileNotFoundError:
            pass
        os.fsync(self.data)
        # Keep a redacted tombstone so a caller cannot recreate or reuse this identity.
        self.record["phase"] = "removed"
        self.save()


def restrict_drop_sql(
    metadata_schema: str,
    binding_id: str,
    data_root: str,
    snapshots: str,
    watermark: str,
    evidence: dict[str, Any],
    dependency_query: str,
) -> str:
    """Build one atomic RESTRICT deletion guarded by native identities and snapshot state."""

    def literal(value: str) -> str:
        """Quote data separately from PostgreSQL identifiers."""
        if "\x00" in value:
            raise OwnershipError()
        return "'" + value.replace("'", "''") + "'"

    def quote(value: str) -> str:
        """Quote observed native relation names as identifiers."""
        if "\x00" in value:
            raise OwnershipError()
        return '"' + value.replace('"', '""') + '"'

    schema = quote(metadata_schema)
    tables = [schema + "." + quote(row[1]) for row in evidence["relations"] if row[2] == "r"]
    if not tables:
        raise OwnershipError()
    oids = ",".join(str(row[0]) for row in evidence["relations"])
    oid = evidence["namespace"][0][0]
    guard = (
        "(SELECT oid FROM pg_namespace WHERE nspname="
        + literal(metadata_schema)
        + " AND nspowner=current_user::regrole AND obj_description(oid,'pg_namespace')="
        + literal("dango:" + binding_id)
        + ") IS DISTINCT FROM "
        + str(oid)
    )
    for key, value in (("version", "1.1"), ("data_path", data_root)):
        guard += (
            " OR (SELECT rtrim(value,'/') FROM "
            + schema
            + ".ducklake_metadata WHERE scope IS NULL AND key="
            + literal(key)
            + ") IS DISTINCT FROM "
            + literal(value)
        )
    guard += " OR (" + snapshots + ") IS DISTINCT FROM " + literal(watermark)
    # DROP TABLE implicitly removes triggers/rules/defaults/constraints too.
    guard += " OR (" + dependency_query + ") IS DISTINCT FROM " + literal(evidence["dependents"])
    guard += (
        " OR EXISTS(SELECT 1 FROM pg_class WHERE relnamespace="
        + str(oid)
        + " AND oid NOT IN ("
        + oids
        + "))"
    )
    guard += (
        " OR (SELECT count(*) FROM pg_class WHERE oid IN ("
        + oids
        + ")) <> "
        + str(len(evidence["relations"]))
    )
    body = (
        "BEGIN LOCK TABLE "
        + ",".join(tables)
        + " IN ACCESS EXCLUSIVE MODE; IF "
        + guard
        + " THEN RAISE EXCEPTION 'catalog_ownership_unverified'; END IF; DROP TABLE "
        + ",".join(tables)
        + " RESTRICT; DROP SCHEMA "
        + schema
        + " RESTRICT; END"
    )
    tag, index = "$dango$", 0
    while tag in body:
        index += 1
        tag = f"$dango_{index}$"
    return "DO " + tag + " " + body + " " + tag
