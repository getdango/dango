"""tests/unit/workspace_support.py

Provide deterministic local-only journal/catalog fixtures, never native acceptance evidence.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dango.operations.contracts import OperationContext, OperationRequest, OutcomeReceipt
from dango.operations.store import OperationStore
from dango.security.native_credentials import SecretRef
from dango.storage.catalogs import CatalogAllocator, NativeCatalogPort
from dango.storage.workspaces import WorkspaceService


class LocalPort(NativeCatalogPort):
    """Persist synthetic effects across processes without native libraries/connections."""

    def __init__(self, root):
        self.root = root
        root.mkdir(exist_ok=True)
        self.calls = []
        self.fail = None

    def prepare(self, binding, credential, checkpoint, evidence=None):
        self.calls.append("prepare")
        native = {"namespace": [[41, "writer", binding.binding_id]], "relations": []}
        (self.root / binding.binding_id).write_text(json.dumps(native))
        checkpoint(native, "prepared")
        if self.fail == "prepare":
            raise RuntimeError("private-native-canary")

    def inspect_owned(self, binding, credential, evidence):
        self.calls.append("inspect")
        actual = json.loads((self.root / binding.binding_id).read_text())
        if actual != evidence:
            raise AssertionError("Synthetic ownership changed")
        return actual

    @contextmanager
    def attach(self, binding, credential, *, read_only):
        self.calls.append("attach")
        yield object()

    def remove_owned(self, binding, credential, evidence, intent, *, resuming):
        self.calls.append("remove")
        path = self.root / binding.binding_id
        if path.exists():
            self.inspect_owned(binding, credential, evidence)
            intent(set())
            path.unlink()
        elif not resuming:
            raise AssertionError("Unrecorded removal")
        if self.fail == "remove":
            raise RuntimeError("private-native-canary")


class Rig:
    """Use actual operation/allocator/workspace code with synthetic native effects only."""

    def __init__(self, root, actor="owner", project="project"):
        self.root, self.actor, self.project = Path(root), actor, project
        for name in ("catalog-state", "data"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.store = OperationStore(
            self.root, clock_epoch_us=lambda: int(self.now.timestamp() * 1000000)
        )
        self.port = LocalPort(self.root / "synthetic-native")
        self.grant = True
        ref = SecretRef(project_id=project, actor_id=actor, credential_id="synthetic", version="v1")
        self.allocator = CatalogAllocator(
            self.root / "catalog-state",
            self.root / "data",
            port=self.port,
            credential_ref=ref,
            catalog_database="synthetic",
            bundle_id="synthetic",
            resolve=lambda value: (
                {
                    "host": "127.0.0.1",
                    "port": "5432",
                    "dbname": "synthetic",
                    "user": "writer",
                    "password": "private-native-canary",
                }
                if value == ref
                else None
            ),
            authorize=lambda context, binding, action: self.grant,
        )
        self.service = WorkspaceService(self.root, self.allocator, self.store, lambda: self.now)

    def context(self, operation=None, **changes):
        return OperationContext(
            **{
                "operation_id": operation or uuid4().hex,
                "project_id": self.project,
                "actor_id": self.actor,
                "capability": "workspace",
                "permission_revision": "r1",
                "trace_id": "trace",
            }
            | changes
        )

    def admit(self, name, payload, *, revision=None, key=None, context=None):
        context = context or self.context()
        result = self.store.begin_operation(
            context,
            OperationRequest(
                operation_name=name,
                payload=payload,
                expected_revision=None if revision is None else str(revision),
                idempotency_key=key,
            ),
        )
        return context.model_copy(update={"operation_id": result.operation_id})

    def allocation(self, *, branch="feature/same", context=None, key=None):
        return self.admit(
            "workspace.allocate",
            {"branch_ref": branch, "code_commit": "commit", "location": "remote"},
            context=context,
            key=key,
        )

    def allocate(self, **kwargs):
        context = self.allocation(**kwargs)
        return context, self.service.allocate(context, "feature/same", "commit", "remote")

    def expiry(self, workspace, revision=None):
        return self.admit(
            "workspace.expire",
            {"workspace_id": workspace.workspace_id},
            revision=workspace.revision if revision is None else revision,
        )

    def finish(self, context):
        return self.store.finalize_operation(
            context, OutcomeReceipt(operation_id=context.operation_id, status="success")
        )

    def rows(self, query, args=()):
        with sqlite3.connect(self.root / ".dango/dango.db") as connection:
            return connection.execute(query, args).fetchall()

    def manifest(self, workspace_id):
        for path in (self.root / ".dango/state/workspaces/resources").glob("*.json"):
            value = json.loads(path.read_text())
            if value["workspace_id"] == workspace_id:
                return path, value
        raise AssertionError("Missing expected workspace manifest")
