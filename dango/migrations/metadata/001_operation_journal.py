"""dango/migrations/metadata/001_operation_journal.py

Create the residual operation journal in the existing metadata database.
"""

from __future__ import annotations

import sqlite3

VERSION = 1
DESCRIPTION = "Create platform operation, effect and lease journal"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create journal tables inside the migration runner's transaction."""
    conn.execute(
        """
        CREATE TABLE platform_operations (
            operation_id TEXT NOT NULL PRIMARY KEY,
            project_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            operation_name TEXT NOT NULL,
            idempotency_key TEXT,
            request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
            permission_revision TEXT NOT NULL,
            expected_revision TEXT,
            resource_revision TEXT,
            result_ref TEXT,
            state TEXT NOT NULL CHECK (state IN ('prepared', 'running', 'finalized')),
            status TEXT CHECK (
                status IN ('success', 'blocked', 'failed', 'cancelled', 'recovery_required')
            ),
            outcome_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (project_id, actor_id, operation_name, idempotency_key),
            CHECK (
                (state IN ('prepared', 'running') AND status IS NULL AND outcome_json IS NULL)
                OR (state = 'finalized' AND status IS NOT NULL AND outcome_json IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE platform_effects (
            operation_id TEXT NOT NULL REFERENCES platform_operations (operation_id),
            effect_id TEXT NOT NULL,
            effect_sequence INTEGER NOT NULL CHECK (effect_sequence >= 0),
            effect_name TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('prepared', 'applied', 'compensated', 'unknown')),
            native_ref TEXT,
            recovery_ref TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (operation_id, effect_id),
            UNIQUE (operation_id, effect_sequence)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE platform_leases (
            project_id TEXT NOT NULL PRIMARY KEY,
            holder_operation_id TEXT NOT NULL REFERENCES platform_operations (operation_id),
            lease_token TEXT NOT NULL,
            expires_at_epoch_us INTEGER NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            updated_at TEXT NOT NULL
        )
        """
    )
