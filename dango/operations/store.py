"""dango/operations/store.py
Persist residual operation state, immutable receipts and server-owned project leases.
"""

from __future__ import annotations

import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from dango.exceptions import MigrationApplicationError
from dango.operations.contracts import (
    OperationContext,
    OperationRequest,
    OutcomeReceipt,
    request_digest,
)
from dango.utils.dango_db import get_connection


def _text(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("Text must be nonblank and contain no control characters.")
    return value


_Id = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_text)]
_Counter = Annotated[int, Field(ge=0, le=2**63 - 1)]
_Model = TypeVar("_Model", bound=BaseModel)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class BeginResult(_Record):
    """Describe admission, an in-flight retry, immutable replay or key conflict."""

    state: Literal["started", "replay", "in_flight", "idempotency_conflict"]
    operation_id: _Id
    receipt: OutcomeReceipt | None = None


class EffectRecord(_Record):
    """Describe one ordered effect and its safe native or recovery references."""

    effect_id: _Id
    sequence: _Counter
    name: _Id
    state: Literal["prepared", "applied", "compensated", "unknown"]
    native_ref: _Id | None = None
    recovery_ref: _Id | None = None


class LeaseBinding(_Record):
    """Bind a holder to a current project lease; this does not fence native writers."""

    project_id: _Id
    operation_id: _Id
    token: _Id
    revision: _Counter
    expires_at_epoch_us: _Counter


class _StoreError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        self.action = {
            "invalid_input": "Supply valid operation records, counters and a positive integer TTL.",
            "operation_denied": "Use the server context for the owning operation.",
            "operation_conflict": "Inspect the operation state; use a new operation for recovery.",
            "effect_conflict": "Inspect the recorded effect identity, order and state before retrying.",
            "lease_conflict": "Acquire a current lease before retrying the lease operation.",
            "store_unavailable": "Inspect the metadata database and retry when it is available.",
        }[code]
        super().__init__(f"{code}: {self.action}")


def _validated(cls: type[_Model], value: object) -> _Model:
    # Revalidate even model_construct/model_copy inputs; models are not authentication.
    if not isinstance(value, cls):
        raise _StoreError("invalid_input")
    try:
        return cls.model_validate(value.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise _StoreError("invalid_input") from None


class OperationStore:
    """Use short SQLite transactions without performing external effects or authentication."""

    def __init__(self, project_root: Path, *, clock_epoch_us: Callable[[], int]) -> None:
        self._project_root = project_root
        self._clock = clock_epoch_us

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            conn = get_connection(self._project_root)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except (sqlite3.Error, OSError, MigrationApplicationError, ValidationError):
            raise _StoreError("store_unavailable") from None
        except (UnicodeError, OverflowError):
            raise _StoreError("invalid_input") from None
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _now(self) -> tuple[int, str]:
        try:
            now = self._clock()
            if type(now) is not int or not 0 <= now <= 2**63 - 1:
                raise ValueError
            stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=now)
            return now, stamp.isoformat(timespec="microseconds")
        except (TypeError, ValueError, OverflowError):
            raise _StoreError("invalid_input") from None

    def _owned(
        self,
        conn: sqlite3.Connection,
        context: OperationContext,
        operation_id: str,
        *,
        active: bool = True,
    ) -> sqlite3.Row:
        if operation_id != context.operation_id:
            raise _StoreError("operation_denied")
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM platform_operations WHERE operation_id=? AND project_id=? AND actor_id=?",
            (operation_id, context.project_id, context.actor_id),
        ).fetchone()
        if row is None:
            raise _StoreError("operation_denied")
        if active and row["state"] == "finalized":
            raise _StoreError("operation_conflict")
        return row

    @staticmethod
    def _receipt(row: sqlite3.Row) -> OutcomeReceipt:
        try:
            receipt = OutcomeReceipt.model_validate_json(row["outcome_json"])
            if receipt.operation_id != row["operation_id"] or receipt.status != row["status"]:
                raise ValueError
            return receipt
        except (ValidationError, ValueError, TypeError):
            raise _StoreError("store_unavailable") from None

    def begin_operation(self, context: OperationContext, request: OperationRequest) -> BeginResult:
        """Atomically admit a prepared operation or return its scoped retry disposition."""
        context = _validated(OperationContext, context)
        request = _validated(OperationRequest, request)
        digest = request_digest(context, request)
        with self._transaction() as conn:
            _, stamp = self._now()
            if request.idempotency_key is not None:
                row = conn.execute(
                    "SELECT * FROM platform_operations WHERE project_id=? AND actor_id=? "
                    "AND operation_name=? AND idempotency_key=?",
                    (
                        context.project_id,
                        context.actor_id,
                        request.operation_name,
                        request.idempotency_key,
                    ),
                ).fetchone()
                if row is not None:
                    if row["request_digest"] != digest:
                        return BeginResult(
                            state="idempotency_conflict", operation_id=row["operation_id"]
                        )
                    if row["state"] == "finalized":
                        return BeginResult(
                            state="replay",
                            operation_id=row["operation_id"],
                            receipt=self._receipt(row),
                        )
                    return BeginResult(state="in_flight", operation_id=row["operation_id"])
            if conn.execute(
                "SELECT 1 FROM platform_operations WHERE operation_id=?", (context.operation_id,)
            ).fetchone():
                return BeginResult(state="idempotency_conflict", operation_id=context.operation_id)
            conn.execute(
                "INSERT INTO platform_operations (operation_id,project_id,actor_id,operation_name,"
                "idempotency_key,request_digest,permission_revision,expected_revision,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    context.operation_id,
                    context.project_id,
                    context.actor_id,
                    request.operation_name,
                    request.idempotency_key,
                    digest,
                    context.permission_revision,
                    request.expected_revision,
                    stamp,
                    stamp,
                ),
            )
            return BeginResult(state="started", operation_id=context.operation_id)

    def mark_running(self, context: OperationContext, operation_id: str) -> None:
        """Compare and swap the owning operation from prepared to running."""
        context = _validated(OperationContext, context)
        with self._transaction() as conn:
            self._owned(conn, context, operation_id)
            _, stamp = self._now()
            if (
                conn.execute(
                    "UPDATE platform_operations SET state='running',updated_at=? WHERE operation_id=? AND state='prepared'",
                    (stamp, operation_id),
                ).rowcount
                != 1
            ):
                raise _StoreError("operation_conflict")

    def record_effect(
        self, context: OperationContext, operation_id: str, effect: EffectRecord
    ) -> None:
        """Prepare ordered effects, then persist forward transitions without external IO."""
        context = _validated(OperationContext, context)
        effect = _validated(EffectRecord, effect)
        with self._transaction() as conn:
            self._owned(conn, context, operation_id)
            _, stamp = self._now()
            old = conn.execute(
                "SELECT * FROM platform_effects WHERE operation_id=? AND effect_id=?",
                (operation_id, effect.effect_id),
            ).fetchone()
            if old is None:
                maximum = conn.execute(
                    "SELECT MAX(effect_sequence) FROM platform_effects WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0]
                if effect.state != "prepared" or (
                    maximum is not None and effect.sequence <= maximum
                ):
                    raise _StoreError("effect_conflict")
                conn.execute(
                    "INSERT INTO platform_effects VALUES (?,?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        effect.effect_id,
                        effect.sequence,
                        effect.name,
                        effect.state,
                        effect.native_ref,
                        effect.recovery_ref,
                        stamp,
                    ),
                )
                return
            if (old["effect_sequence"], old["effect_name"]) != (effect.sequence, effect.name):
                raise _StoreError("effect_conflict")
            if (old["state"], old["native_ref"], old["recovery_ref"]) == (
                effect.state,
                effect.native_ref,
                effect.recovery_ref,
            ):
                return
            transitions = {
                "prepared": {"applied", "unknown", "compensated"},
                "applied": {"compensated"},
                "unknown": {"compensated"},
                "compensated": set(),
            }
            if effect.state not in transitions[old["state"]]:
                raise _StoreError("effect_conflict")
            conn.execute(
                "UPDATE platform_effects SET state=?,native_ref=?,recovery_ref=? WHERE operation_id=? AND effect_id=?",
                (
                    effect.state,
                    effect.native_ref,
                    effect.recovery_ref,
                    operation_id,
                    effect.effect_id,
                ),
            )

    def finalize_operation(
        self, context: OperationContext, receipt: OutcomeReceipt
    ) -> OutcomeReceipt:
        """Persist a validated final receipt once; replay identical finalization exactly."""
        context = _validated(OperationContext, context)
        receipt = _validated(OutcomeReceipt, receipt)
        try:
            receipt_json = receipt.model_dump_json()
        except ValueError:
            raise _StoreError("invalid_input") from None
        with self._transaction() as conn:
            row = self._owned(conn, context, receipt.operation_id, active=False)
            if row["state"] == "finalized":
                stored = self._receipt(row)
                if stored != receipt:
                    raise _StoreError("operation_conflict")
                return stored
            _, stamp = self._now()
            conn.execute(
                "UPDATE platform_operations SET state='finalized',status=?,outcome_json=?,resource_revision=?,result_ref=?,updated_at=? WHERE operation_id=? AND state IN ('prepared','running')",
                (
                    receipt.status,
                    receipt_json,
                    receipt.resource_revision,
                    receipt.result_ref,
                    stamp,
                    receipt.operation_id,
                ),
            )
            return receipt

    @staticmethod
    def _expiry(now: int, ttl_seconds: int) -> int:
        if (
            type(ttl_seconds) is not int
            or ttl_seconds <= 0
            or ttl_seconds > (2**63 - 1 - now) // 1_000_000
        ):
            raise _StoreError("invalid_input")
        return now + ttl_seconds * 1_000_000

    @staticmethod
    def _binding(row: sqlite3.Row) -> LeaseBinding:
        return LeaseBinding(
            project_id=row["project_id"],
            operation_id=row["holder_operation_id"],
            token=row["lease_token"],
            revision=row["revision"],
            expires_at_epoch_us=row["expires_at_epoch_us"],
        )

    def acquire_lease(
        self, context: OperationContext, operation_id: str, ttl_seconds: int
    ) -> LeaseBinding:
        """Acquire an absent or expired project lease, retaining monotonic takeover history."""
        context = _validated(OperationContext, context)
        with self._transaction() as conn:
            self._owned(conn, context, operation_id)
            now, stamp = self._now()
            expires = self._expiry(now, ttl_seconds)
            row = conn.execute(
                "SELECT * FROM platform_leases WHERE project_id=?", (context.project_id,)
            ).fetchone()
            if row is not None and row["expires_at_epoch_us"] > now:
                raise _StoreError("lease_conflict")
            revision = 0 if row is None else row["revision"] + 1
            if revision > 2**63 - 1:
                raise _StoreError("lease_conflict")
            lease = LeaseBinding(
                project_id=context.project_id,
                operation_id=operation_id,
                token=secrets.token_urlsafe(32),
                revision=revision,
                expires_at_epoch_us=expires,
            )
            conn.execute(
                "INSERT INTO platform_leases VALUES (?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET holder_operation_id=excluded.holder_operation_id,lease_token=excluded.lease_token,expires_at_epoch_us=excluded.expires_at_epoch_us,revision=excluded.revision,updated_at=excluded.updated_at",
                (lease.project_id, lease.operation_id, lease.token, expires, revision, stamp),
            )
            return lease

    def _current_lease(
        self, conn: sqlite3.Connection, context: OperationContext, lease: LeaseBinding, now: int
    ) -> sqlite3.Row:
        if lease.project_id != context.project_id or lease.operation_id != context.operation_id:
            raise _StoreError("operation_denied")
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM platform_leases WHERE project_id=?", (context.project_id,)
        ).fetchone()
        if row is None or row["expires_at_epoch_us"] <= now or self._binding(row) != lease:
            raise _StoreError("lease_conflict")
        return row

    def renew_lease(
        self, context: OperationContext, lease: LeaseBinding, ttl_seconds: int
    ) -> LeaseBinding:
        """Renew only the exact current unexpired binding using server time and a new revision."""
        context = _validated(OperationContext, context)
        lease = _validated(LeaseBinding, lease)
        with self._transaction() as conn:
            self._owned(conn, context, lease.operation_id)
            now, stamp = self._now()
            expires = self._expiry(now, ttl_seconds)
            self._current_lease(conn, context, lease, now)
            if lease.revision == 2**63 - 1:
                raise _StoreError("lease_conflict")
            renewed = LeaseBinding(
                project_id=lease.project_id,
                operation_id=lease.operation_id,
                token=lease.token,
                revision=lease.revision + 1,
                expires_at_epoch_us=expires,
            )
            conn.execute(
                "UPDATE platform_leases SET expires_at_epoch_us=?,revision=?,updated_at=? WHERE project_id=? AND lease_token=? AND revision=?",
                (expires, renewed.revision, stamp, lease.project_id, lease.token, lease.revision),
            )
            return renewed

    def release_lease(self, context: OperationContext, lease: LeaseBinding) -> None:
        """Expire the current binding without deleting its revision, including after finalization."""
        context = _validated(OperationContext, context)
        lease = _validated(LeaseBinding, lease)
        with self._transaction() as conn:
            self._owned(conn, context, lease.operation_id, active=False)
            now, stamp = self._now()
            self._current_lease(conn, context, lease, now)
            conn.execute(
                "UPDATE platform_leases SET expires_at_epoch_us=?,updated_at=? WHERE project_id=? AND lease_token=? AND revision=?",
                (now, stamp, lease.project_id, lease.token, lease.revision),
            )
