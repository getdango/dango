"""tests/integration/input_sql_fixture.py

Observe one rejected-write fixture and recover only a proven own residual after real expiry refusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from dango.storage.catalog_ownership import OwnershipError, directory
from dango.storage.workspaces import WorkspaceError


class FixtureRefusal(RuntimeError):
    """Refuse ambiguous test ownership without exposing native or credential diagnostics."""

    def __init__(self):
        super().__init__("fixture_ownership_unverified")


def require(condition):
    """Keep all conditional fixture cleanup assertions fail-closed even under optimized Python."""
    if not condition:
        raise FixtureRefusal()


def native_references(rig, binding):
    """Read every registered data/delete path, including historical references, without mutation."""
    schema = '"' + binding.metadata_schema + '"'
    result = {}
    for kind in ("data", "delete"):
        result[kind] = rig.native.sql(
            "SELECT s.path,s.path_is_relative,t.path,t.path_is_relative,"
            "f.path,f.path_is_relative,f.end_snapshot FROM "
            + schema
            + ".ducklake_"
            + kind
            + "_file f JOIN "
            + schema
            + ".ducklake_table t USING(table_id) JOIN "
            + schema
            + ".ducklake_schema s USING(schema_id) ORDER BY 1,3,5,7"
        )
    return result


def checkpoint_report(report, rig, output):
    """Durably retain the existing native report before any conditional fixture unlink."""
    value = report | {
        "fixture_lifecycle": getattr(getattr(rig, "sql_guard", None), "evidence", {}),
        "expiry_diagnostics": getattr(rig, "expiry_diagnostics", {}),
    }
    temporary = output.with_name(output.name + ".fixture-pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sql_oracle(rig):
    """Run the full adversarial04 oracle in a fresh tracked authorized workspace."""
    from input_observations import target

    from tests.unit.test_input_copy_sql import exercise_sql_roundtrip

    workspace = target(rig)
    owner = rig.admit("fixture.structured-sql", {})
    binding = rig.native.current(workspace.workspace_id)
    rig.sql_guard = RejectedWrite(
        binding, lambda: native_references(rig, binding), {}, rig.fixture_checkpoint
    )
    with rig.allocator.attach(owner, binding, read_only=False) as connection:
        proof = exercise_sql_roundtrip(
            connection, rig.native.root / "sql-api", rig.sql_guard.negative
        )
    rig.finish(owner)
    return proof


class RejectedWrite:
    """Scope identities to one exclusively owned native04 fixture, never a general orphan collector."""

    def __init__(self, binding, references, evidence, checkpoint):
        self.binding, self.root = binding, Path(binding.data_root_ref)
        self.read_references, self.evidence, self.checkpoint = references, evidence, checkpoint
        require(self.root.is_absolute() and str(self.root) == binding.data_root_ref)
        self.initial_root = None
        self.used = False
        evidence.update(
            workspace_id=binding.workspace_id,
            binding_id=binding.binding_id,
            project_id=binding.project_id,
            actor_id=binding.owner_actor_id,
            data_root=binding.data_root_ref,
            metadata_schema=binding.metadata_schema,
        )

    def references(self):
        """Freeze each independent raw reference observation rather than retaining mutable aliases."""
        return json.loads(json.dumps(self.read_references()))

    @staticmethod
    def _identity(fd, *, folder=False):
        info = os.fstat(fd)
        require(info.st_uid == os.getuid())
        require(stat.S_ISDIR(info.st_mode) if folder else stat.S_ISREG(info.st_mode))
        require(folder or info.st_nlink == 1)
        return {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }

    @classmethod
    def _file(cls, parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = cls._identity(fd)
            digest = hashlib.sha256()
            while chunk := os.read(fd, 65536):
                digest.update(chunk)
            require(cls._identity(fd) == before)
            return before | {"sha256": digest.hexdigest()}
        finally:
            os.close(fd)

    def snapshot(self):
        """Open every component without symlinks and retain all regular-file and directory identities."""
        fd = directory(self.root)
        try:
            root = self._identity(fd, folder=True)
            identity = {k: root[k] for k in ("device", "inode", "uid", "mode")}
            if self.initial_root is None:
                self.initial_root = identity
            require(identity == self.initial_root)
            files, folders = {}, {"": identity}

            def walk(parent, prefix):
                for name in sorted(os.listdir(parent)):
                    relative = prefix + name
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                        )
                        try:
                            item = self._identity(child, folder=True)
                            folders[relative] = {
                                k: item[k] for k in ("device", "inode", "uid", "mode")
                            }
                            walk(child, relative + "/")
                        finally:
                            os.close(child)
                    else:
                        files[relative] = self._file(parent, name)

            walk(fd, "")
            return {"root": identity, "files": files, "folders": folders}
        finally:
            os.close(fd)

    def paths(self, references):
        """Resolve all registered path triples, including references to currently missing old files."""
        result = set()
        require(set(references) == {"data", "delete"})
        for rows in references.values():
            for row in rows:
                require(len(row) == 7)
                path = self.root
                for value, relative in zip(row[:6:2], row[1:6:2], strict=True):
                    require(type(value) is str and type(relative) is bool)
                    part = Path(value)
                    require(".." not in part.parts and not any(ord(c) < 32 for c in value))
                    require(not (relative and part.is_absolute()))
                    require(relative or part.is_absolute())
                    path = path / part if relative else part
                    require(path.is_absolute() and path.is_relative_to(self.root))
                relative = str(path.relative_to(self.root))
                require(relative != ".")
                result.add(relative)
        return result

    @contextmanager
    def negative(self):
        """Record before/after facts only; never remove a file before actual service expiry fails."""
        self.before, self.refs = self.snapshot(), self.references()
        self.paths(self.refs)
        self.evidence.update(before=self.before, references_before=self.refs)
        self.checkpoint()
        yield
        self.after, refs = self.snapshot(), self.references()
        self.evidence.update(after_negative=self.after, references_after=refs, removed=[])
        require(refs == self.refs)
        require(all(self.after["files"].get(k) == v for k, v in self.before["files"].items()))
        require(self.after["folders"] == self.before["folders"])
        self.checkpoint()

    def _remove(self, name, expected):
        fd = directory(self.root)
        try:
            for part in Path(name).parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            require(self._file(fd, Path(name).name) == expected)
            current = os.stat(Path(name).name, dir_fd=fd, follow_symlinks=False)
            require(
                (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                == tuple(expected[k] for k in ("device", "inode", "size", "mtime_ns", "ctime_ns"))
            )
            os.unlink(Path(name).name, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)

    def recover(self, expected, failure):
        """Delete at most one new own test file only after a proven exact inventory-stage refusal."""
        require(not self.used)
        self.used = True
        require(failure == "OwnedWorkspace.removal_intent:OwnershipError")
        refs = self.references()
        current = self.snapshot()
        self.evidence.update(
            before_conditional_cleanup=current,
            references_at_cleanup=refs,
            inventory_expected=sorted(expected),
        )
        require(refs == self.refs and current == self.after)
        candidates = set(current["files"]) - set(self.before["files"])
        require(len(candidates) == 1 and set(current["files"]) - expected == candidates)
        require(expected == set(self.before["files"]))
        require(not (candidates & self.paths(refs)))
        name = next(iter(candidates))
        require(
            name.endswith(".parquet")
            and str(Path(name).parent)
            in {str(Path(p).parent) for p in self.before["files"]} | {"."}
        )
        self.evidence["pre_unlink_receipt"] = {
            "removed": [],
            "actual_expiry_failure": dict(self.evidence["actual_expiry_failure"]),
            "failed_operation": dict(self.evidence["failed_operation"]),
        }
        self.checkpoint()
        # Recheck references and the complete inventory at the actual unlink boundary.
        require(self.references() == self.refs and self.snapshot() == current)
        self._remove(name, current["files"][name])
        self.evidence["removed"].append({"path": name, "identity": current["files"][name]})
        require(self.references() == self.refs and self.snapshot() == self.before)
        self.evidence["after_conditional_cleanup"] = self.snapshot()
        self.checkpoint()


def expire_fixture(rig, workspace):
    """Observe normal expiry first; only the recorded negative fixture may recover the same operation."""
    owner = rig.admit(
        "workspace.expire", {"workspace_id": workspace.workspace_id}, revision=workspace.revision
    )
    guard = getattr(rig, "sql_guard", None)
    candidate = guard is not None and guard.binding.workspace_id == workspace.workspace_id
    diagnostics = {
        "workspace_id": workspace.workspace_id,
        "stage": "workspace_service_expire",
        "operation_id": owner.operation_id,
        "expected_revision": workspace.revision,
    }
    rig.expiry_diagnostics = diagnostics
    original = rig.allocator.port.remove_owned
    observed = {}

    def remove(binding, credential, evidence, intent, *, resuming):
        def inventory(files):
            observed.update(expected=set(files), stage="OwnedWorkspace.removal_intent")
            try:
                return intent(files)
            except OwnershipError:
                observed["failure"] = "OwnedWorkspace.removal_intent:OwnershipError"
                raise

        return original(binding, credential, evidence, inventory, resuming=resuming)

    if candidate:
        rig.allocator.port.remove_owned = remove
    try:
        try:
            receipt = rig.service.expire(owner, workspace.workspace_id, workspace.revision)
        except WorkspaceError as error:
            diagnostics.update(
                error_code=error.code, stage=observed.get("stage", diagnostics["stage"])
            )
            if not candidate:
                raise
            guard.evidence["actual_expiry_failure"] = dict(diagnostics) | {
                "classification": observed.get("failure")
            }
            phase = next(
                json_value["phase"]
                for json_value in (
                    json.loads(p.read_text()) for p in rig.allocator.state_root.glob("*.json")
                )
                if json_value["binding"]["workspace_id"] == workspace.workspace_id
            )
            inspection = rig.store.inspect_effect(owner, owner.operation_id, "workspace.expire")
            guard.evidence["failed_operation"] = {
                "state": inspection.state,
                "receipt": None if inspection.receipt is None else inspection.receipt.model_dump(),
                "allocator_phase": phase,
            }
            require(error.code == "workspace_recovery_required" and phase == "prepared")
            require(inspection.state == "prepared" and inspection.receipt is None)
            require(
                inspection.effect is not None
                and inspection.effect.state == "prepared"
                and inspection.effect.native_ref == workspace.workspace_id
            )
            guard.evidence["expiry_effect"] = inspection.effect.model_dump()
            try:
                with rig.allocator._scope(owner, workspace.workspace_id, guard.binding) as owned:
                    rig.allocator._access(owner, guard.binding, "remove")
                    require(owned.record is not None and owned.record["phase"] == "prepared")
                    owned.validate_root()
                    guard.recover(observed.get("expected", set()), observed.get("failure"))
            except (OSError, OwnershipError):
                raise FixtureRefusal() from None
            receipt = rig.service.expire(owner, workspace.workspace_id, workspace.revision)
            guard.evidence["same_operation_retry"] = receipt.model_dump()
            guard.checkpoint()
        if candidate and not guard.used:
            guard.evidence["normal_expiry_receipt"] = receipt.model_dump()
            guard.checkpoint()
        diagnostics["stage"] = "post_expiry_absence_checks"
        return receipt
    finally:
        if candidate:
            rig.allocator.port.remove_owned = original
