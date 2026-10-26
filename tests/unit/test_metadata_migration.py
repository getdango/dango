"""tests/unit/test_metadata_migration.py

Verify the metadata journal schema on isolated SQLite projects.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest

from dango.exceptions import MigrationApplicationError
from dango.migrations import MigrationRunner
from dango.operations.contracts import OutcomeReceipt
from dango.utils.dango_db import _ensure_schema, get_connection, get_dango_db_path

pytestmark = pytest.mark.unit
MIGRATIONS = Path(__file__).resolve().parents[2] / "dango/migrations/metadata"
NOW = "2026-10-26T00:00:00+00:00"


def _legacy_connection(project: Path) -> sqlite3.Connection:
    """Prepare pre-bootstrap metadata explicitly for migration-only fixtures."""
    db_path = get_dango_db_path(project)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    conn.commit()
    return conn


def _runner(project: Path, directory: Path = MIGRATIONS, name: str = "metadata") -> MigrationRunner:
    return MigrationRunner(get_dango_db_path(project), name, directory)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _operation(conn: sqlite3.Connection, operation_id: str = "op", **overrides: object) -> None:
    values: dict[str, object] = {
        "operation_id": operation_id,
        "project_id": "project",
        "actor_id": "actor",
        "operation_name": "publish",
        "idempotency_key": "key",
        "request_digest": "a" * 64,
        "permission_revision": "permission-1",
        "state": "prepared",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO platform_operations ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _effect(
    conn: sqlite3.Connection, effect_id: str = "effect", sequence: int = 0, **kw: object
) -> None:
    conn.execute(
        "INSERT INTO platform_effects "
        "(operation_id, effect_id, effect_sequence, effect_name, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            kw.get("operation_id", "op"),
            effect_id,
            sequence,
            "publish",
            kw.get("state", "prepared"),
            NOW,
        ),
    )


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    legacy = _legacy_connection(tmp_path)
    legacy.close()
    assert [item.version for item in _runner(tmp_path).apply_pending()] == [1]
    conn = get_connection(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def test_metadata_migration_preserves_legacy_governance_row(tmp_path: Path) -> None:
    conn = _legacy_connection(tmp_path)
    try:
        conn.execute(
            "INSERT INTO schema_baselines VALUES (?, ?, ?, ?, ?)",
            ("legacy", "customers", "email", "VARCHAR", NOW),
        )
        conn.commit()
        before = list(
            conn.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")
        )
        tables = _tables(conn)
        _runner(tmp_path).apply_pending()
        assert _tables(conn) - tables == {
            "_migrations",
            "platform_operations",
            "platform_effects",
            "platform_leases",
        }
        assert [tuple(row) for row in conn.execute("SELECT * FROM schema_baselines")] == [
            ("legacy", "customers", "email", "VARCHAR", NOW)
        ]
        after = list(conn.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"))
        assert {row[0] for row in before}.issubset({row[0] for row in after})
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_metadata_version_one_applies_once(tmp_path: Path) -> None:
    conn = _legacy_connection(tmp_path)
    conn.close()
    runner = _runner(tmp_path)
    assert [item.version for item in runner.apply_pending()] == [1]
    first = runner.status().applied
    assert len(first) == 1 and first[0].description and first[0].applied_at
    assert runner.apply_pending() == []
    assert runner.status().applied == first
    assert runner.current_version() == 1


def test_metadata_stream_does_not_touch_auth_or_scheduler_db(tmp_path: Path) -> None:
    conn = _legacy_connection(tmp_path)
    conn.close()
    siblings = [tmp_path / ".dango" / f"{name}.db" for name in ("auth", "scheduler")]
    for path in siblings:
        with sqlite3.connect(path) as sibling:
            sibling.execute("CREATE TABLE sentinel (value TEXT)")
            sibling.execute("INSERT INTO sentinel VALUES ('preserved')")
    before = [path.read_bytes() for path in siblings]
    _runner(tmp_path).apply_pending()
    assert [path.read_bytes() for path in siblings] == before
    assert not (tmp_path / ".dango/metadata.db").exists()


def test_metadata_version_stream_is_global_and_preserves_unexplained_history(
    tmp_path: Path,
) -> None:
    conn = _legacy_connection(tmp_path)
    try:
        runner = _runner(tmp_path)
        runner.ensure_migrations_table(conn)
        conn.execute("INSERT INTO _migrations VALUES (1, 'unexplained legacy entry', ?)", (NOW,))
        conn.commit()
        inventory = [tuple(row) for row in conn.execute("SELECT * FROM _migrations")]
        assert _runner(tmp_path, name="another-label").apply_pending() == []
        assert [tuple(row) for row in conn.execute("SELECT * FROM _migrations")] == inventory
        assert "platform_operations" not in _tables(conn)
        # A matching integer is inventoried, never claimed as compatible journal schema.
    finally:
        conn.close()


def test_operation_key_scopes_actor_project_and_operation(journal: sqlite3.Connection) -> None:
    _operation(journal)
    with pytest.raises(sqlite3.IntegrityError):
        _operation(journal, "duplicate")
    for index, field in enumerate(("actor_id", "project_id", "operation_name")):
        _operation(journal, f"scope-{index}", **{field: "different"})
    assert journal.execute("SELECT count(*) FROM platform_operations").fetchone()[0] == 4


def test_multiple_read_operations_allow_null_idempotency_key(journal: sqlite3.Connection) -> None:
    for operation_id in ("read-1", "read-2"):
        _operation(journal, operation_id, operation_name="inspect", idempotency_key=None)
    assert journal.execute("SELECT count(*) FROM platform_operations").fetchone()[0] == 2


def test_duplicate_effect_id_or_sequence_is_rejected(journal: sqlite3.Connection) -> None:
    _operation(journal)
    _effect(journal)
    for effect_id, sequence in (("effect", 1), ("other", 0)):
        with pytest.raises(sqlite3.IntegrityError):
            _effect(journal, effect_id, sequence)
    _operation(journal, "op-2", idempotency_key="another")
    _effect(journal, operation_id="op-2")
    assert journal.execute("SELECT count(*) FROM platform_effects").fetchone()[0] == 2


def test_invalid_status_and_negative_lease_revision_are_rejected(
    journal: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _operation(journal, state="finalized", status="unknown", outcome_json="{}")
    _operation(journal)
    with pytest.raises(sqlite3.IntegrityError):
        journal.execute(
            "INSERT INTO platform_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("project", "op", "token", 100, -1, NOW),
        )
    journal.execute(
        "INSERT INTO platform_leases VALUES (?, ?, ?, ?, ?, ?)",
        ("project", "op", "token", 100, 0, NOW),
    )
    with pytest.raises(sqlite3.IntegrityError):
        journal.execute(
            "INSERT INTO platform_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("project", "op", "token-2", 200, 1, NOW),
        )


@pytest.mark.parametrize("state", ["prepared", "running", "finalized"])
@pytest.mark.parametrize("status", [None, "success"])
@pytest.mark.parametrize("outcome", [None, '{"operation_id":"op","status":"success"}'])
def test_running_operation_has_no_final_outcome_and_finalized_requires_both_fields(
    journal: sqlite3.Connection, state: str, status: str | None, outcome: str | None
) -> None:
    valid = (state == "finalized" and status is not None and outcome is not None) or (
        state != "finalized" and status is None and outcome is None
    )
    if valid:
        _operation(journal, state=state, status=status, outcome_json=outcome)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            _operation(journal, state=state, status=status, outcome_json=outcome)


def test_all_t01_outcome_statuses_are_accepted(journal: sqlite3.Connection) -> None:
    statuses = get_args(OutcomeReceipt.model_fields["status"].annotation)
    assert set(statuses) == {"success", "blocked", "failed", "cancelled", "recovery_required"}
    for status in statuses:
        fields = {} if status == "success" else {"error_code": "test", "action": "Retry safely."}
        receipt = OutcomeReceipt(operation_id=status, status=status, **fields)
        _operation(
            journal,
            status,
            idempotency_key=status,
            state="finalized",
            status=status,
            outcome_json=receipt.model_dump_json(),
        )
    assert journal.execute("SELECT count(*) FROM platform_operations").fetchone()[0] == 5


def test_effect_foreign_key_enforced_on_dango_connection(journal: sqlite3.Connection) -> None:
    assert journal.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        _effect(journal)
    with pytest.raises(sqlite3.IntegrityError):
        journal.execute(
            "INSERT INTO platform_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("project", "missing", "token", 100, 0, NOW),
        )
    _operation(journal)
    _effect(journal)
    with pytest.raises(sqlite3.IntegrityError):
        journal.execute("DELETE FROM platform_operations WHERE operation_id='op'")


@pytest.mark.parametrize(
    "values",
    [{"request_digest": "short"}, {"state": "unknown"}, {"operation_id": None}, {"actor_id": None}],
)
def test_invalid_operation_values_are_rejected(journal: sqlite3.Connection, values: dict) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _operation(journal, **values)


def test_invalid_effect_values_are_rejected(journal: sqlite3.Connection) -> None:
    _operation(journal)
    for values in ({"sequence": -1}, {"state": "invalid"}, {"effect_id": None}):
        with pytest.raises(sqlite3.IntegrityError):
            _effect(journal, **values)
    for index, state in enumerate(("prepared", "applied", "compensated", "unknown")):
        _effect(journal, str(index), index, state=state)


def test_runner_rollback_preserves_prior_schema_and_version(tmp_path: Path) -> None:
    conn = _legacy_connection(tmp_path)
    conn.close()
    _runner(tmp_path).apply_pending()
    directory = tmp_path / "failing-migrations"
    directory.mkdir()
    shutil.copyfile(MIGRATIONS / "001_operation_journal.py", directory / "001_operation_journal.py")
    (directory / "002_failure.py").write_text(
        'VERSION = 2\nDESCRIPTION = "Independent failure fixture"\n'
        "def upgrade(conn):\n"
        '    conn.execute("CREATE TABLE transient_table (id INTEGER)")\n'
        '    conn.execute("ALTER TABLE platform_operations ADD COLUMN transient TEXT")\n'
        "    conn.execute(\"UPDATE platform_operations SET actor_id = 'changed'\")\n"
        '    raise RuntimeError("fixture failure")\n'
    )
    conn = get_connection(tmp_path)
    try:
        _operation(conn)
        conn.commit()
        before = [tuple(row) for row in conn.execute("SELECT * FROM sqlite_master ORDER BY name")]
        history = [tuple(row) for row in conn.execute("SELECT * FROM _migrations")]
        runner = _runner(tmp_path, directory)
        with pytest.raises(MigrationApplicationError, match="fixture failure"):
            runner.apply_pending()
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM sqlite_master ORDER BY name")
        ] == before
        assert [tuple(row) for row in conn.execute("SELECT * FROM _migrations")] == history
        assert conn.execute("SELECT actor_id FROM platform_operations").fetchone()[0] == "actor"
        assert runner.get_applied_versions() == [1]
        assert [item.version for item in runner.get_pending()] == [2]
    finally:
        conn.close()


def test_schema_import_has_no_filesystem_or_connection_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with patch("sqlite3.connect", side_effect=AssertionError("unexpected connection")) as connect:
        with patch.object(Path, "mkdir", side_effect=AssertionError("unexpected directory")):
            with patch.object(Path, "write_text", side_effect=AssertionError("unexpected write")):
                package = importlib.import_module("dango.migrations.metadata")
                importlib.reload(package)
                module = importlib.import_module("dango.migrations.metadata.001_operation_journal")
                importlib.reload(module)
    connect.assert_not_called()
    assert list(tmp_path.iterdir()) == []
    assert module.VERSION == 1 and module.DESCRIPTION
