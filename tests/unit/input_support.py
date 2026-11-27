"""tests/unit/input_support.py

Provide local-only capture fixtures over real admission, workspace and pin persistence.
"""

import fcntl
import hashlib
import json
import os
from uuid import uuid4

from dango.storage.input_state import (
    AcceptedFixture,
    Column,
    Guardian,
    InputCaptureError,
    Relation,
    capture_record,
    directory,
    fingerprint,
)
from dango.storage.inputs import InputCaptureService
from dango.storage.workspace_state import LockedRecord, current_binding
from tests.unit.workspace_support import Rig


class LocalCopy:
    """Inject byte/failure outcomes; none of these checks claim native snapshot evidence."""

    def __init__(self):
        self.calls = []
        self.estimate = 100
        self.actual = 90
        self.fail = None

    def preflight(self, *args):
        self.calls.append("preflight")
        if self.fail == "collision":
            raise InputCaptureError("capture_collision")

    def export(self, context, manifest, relations, identity, maximum, gate, estimate_ready):
        self.calls.append("export")
        gate()
        estimate_ready(self.estimate)
        if self.estimate > maximum:
            raise InputCaptureError("capture_quota_exceeded")
        if self.fail in {"snapshot", "schema"}:
            raise InputCaptureError(
                "capture_source_unavailable"
                if self.fail == "snapshot"
                else "capture_schema_changed"
            )
        gate()
        return self.estimate

    def inventory(self, *args):
        return ()

    def copy_one(self, context, binding, relation, identity, index):
        self.calls.append("copy")
        if self.fail == "interrupt":
            raise KeyboardInterrupt()
        return relation.table_uuid, self.actual

    def cleanup(self, *args):
        self.calls.append("cleanup")


class InputRig(Rig):
    """Install exactly one immutable accepted fixture and a genuinely held local lifetime lock."""

    def __init__(self, root):
        super().__init__(root)
        self.source_context, self.source = self.allocate()
        self.target_context, self.target = self.allocate()
        binding, _ = current_binding(self.allocator, self.source_context, self.source.workspace_id)
        columns = (Column(name="id", type="INTEGER", nullable=False),)
        self.manifest = AcceptedFixture(
            kind="accepted-input-fixture-v1",
            fixture_id="p0-selected-inputs-v1",
            accepted_release_id="p0-accepted-v1",
            project_id=self.project,
            actor_id=self.actor,
            catalog_id="accepted-production",
            binding=binding,
            snapshot_id=2,
            relations=tuple(
                Relation(
                    relation_id=name,
                    schema_name=name,
                    table_name="orders",
                    table_uuid=uuid4().hex,
                    columns=columns,
                    schema_fingerprint=fingerprint(columns),
                    source_version_ref="source-v1-" + name,
                    checkpoint_ref="checkpoint-v1-" + name,
                )
                for name in ("sales", "finance")
            ),
            retention_mode="exclusive_fixture_lifetime",
            maintenance_mode="disabled",
            lifetime_nonce=uuid4().hex,
        )
        fd = directory(self.root)
        try:
            LockedRecord(fd, "accepted").save(self.manifest)
            self.guard = os.open(
                "lifetime.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
            )
            guardian = Guardian(
                kind="input-fixture-lifetime-v1",
                nonce=self.manifest.lifetime_nonce,
                project_id=self.project,
                actor_id=self.actor,
                source_workspace_id=self.source.workspace_id,
                source_binding_id=binding.binding_id,
                manifest_sha256=hashlib.sha256(
                    self.manifest.model_dump_json().encode()
                ).hexdigest(),
            )
            os.write(self.guard, guardian.model_dump_json().encode())
            os.fsync(self.guard)
            fcntl.flock(self.guard, fcntl.LOCK_EX)
        finally:
            os.close(fd)
        self.capture_service = InputCaptureService(
            self.root, self.allocator, self.service, lambda: self.now
        )
        self.copy = LocalCopy()
        self.capture_service.copy = self.copy

    def close(self):
        os.close(self.guard)

    def capture_context(self, *, relations=("sales",), maximum=1000, key=None, context=None):
        return self.admit(
            "inputs.capture",
            {
                "workspace_id": self.target.workspace_id,
                "accepted_release_id": "p0-accepted-v1",
                "relation_ids": list(relations),
                "max_bytes": maximum,
            },
            key=key,
            context=context,
        )

    def capture(self, context=None, *, relations=("sales",), maximum=1000):
        context = context or self.capture_context(relations=relations, maximum=maximum)
        return context, self.capture_service.capture_inputs(
            context, self.target.workspace_id, "p0-accepted-v1", relations, maximum
        )

    def record(self, identity):
        path = self.root / ".dango/state/inputs/captures" / (identity + ".json")
        return path, json.loads(path.read_text())

    def edit(self, identity, changes):
        from dango.storage.input_state import CaptureRecord

        with capture_record(self.root, identity) as record:
            record.save(record.read(CaptureRecord).model_copy(update=changes))

    def observation(self):
        """Independently snapshot persisted JSON/pins/raw SQL and all synthetic native/copy calls."""
        return {
            "json": {
                str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*.json")
            },
            "operations": self.rows("SELECT * FROM platform_operations ORDER BY operation_id"),
            "effects": self.rows("SELECT * FROM platform_effects ORDER BY operation_id,effect_id"),
            "native_calls": tuple(self.port.calls),
            "copy_calls": tuple(self.copy.calls),
        }
