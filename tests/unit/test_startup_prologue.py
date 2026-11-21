"""tests/unit/test_startup_prologue.py

The shared serve/web startup prologue: step order, error labels, hook arguments and locking.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.exceptions import DbtLockError, MigrationError, VersionMismatchError
from dango.platform.common.startup_prologue import (
    PrologueError,
    load_project_env,
    run_local_prologue,
)
from dango.utils.dbt_lock import DbtLock

pytestmark = pytest.mark.unit

_STARTUP = "dango.platform.common.startup"


def test_steps_run_in_order_and_return_the_migrations(tmp_path: Path) -> None:
    events: list[str] = []
    with (
        patch(f"{_STARTUP}.check_duckdb_version_alignment", side_effect=lambda: events.append("a")),
        patch(f"{_STARTUP}.rotate_logs", side_effect=lambda _r: events.append("r")),
        patch(
            f"{_STARTUP}.run_pending_migrations",
            side_effect=lambda _r: events.append("m") or {"auth": []},
        ),
        patch(
            f"{_STARTUP}.cleanup_stale_dbt_lock", side_effect=lambda _r: events.append("l") or False
        ),
        patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=lambda _r: events.append("s")),
    ):
        applied = run_local_prologue(tmp_path, after_migrations=lambda _f: events.append("hook"))
    assert events == ["a", "r", "m", "l", "hook", "s"]
    assert applied == {"auth": []}


def test_hook_receives_whether_a_stale_lock_was_removed(tmp_path: Path) -> None:
    seen: list[bool] = []
    with (
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=True),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
    ):
        run_local_prologue(tmp_path, after_migrations=seen.append)
    assert seen == [True]


@pytest.mark.parametrize(
    ("target", "error", "label", "later"),
    [
        (
            "check_duckdb_version_alignment",
            VersionMismatchError("x"),
            "DuckDB version mismatch",
            "rotate_logs",
        ),
        ("run_pending_migrations", MigrationError("x"), "Migration failed", "ensure_dbt_schemas"),
        ("ensure_dbt_schemas", RuntimeError("x"), "Schema setup failed", None),
    ],
)
def test_a_failing_step_raises_a_labelled_error_and_stops(
    tmp_path: Path, target: str, error: Exception, label: str, later: str | None
) -> None:
    with (
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs") as rotate,
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.ensure_dbt_schemas") as schemas,
    ):
        with patch(f"{_STARTUP}.{target}", side_effect=error):
            with pytest.raises(PrologueError) as info:
                run_local_prologue(tmp_path)
        not_reached = {"rotate_logs": rotate, "ensure_dbt_schemas": schemas}.get(later or "")
    assert info.value.label == label
    assert info.value.cause is error
    if not_reached is not None:
        not_reached.assert_not_called()


def test_an_unexpected_alignment_error_is_not_relabelled(tmp_path: Path) -> None:
    with patch(f"{_STARTUP}.check_duckdb_version_alignment", side_effect=OSError("disk")):
        with pytest.raises(OSError, match="disk"):
            run_local_prologue(tmp_path)


def test_schema_step_is_unlocked_by_default_and_locked_on_request(tmp_path: Path) -> None:
    states: list[bool] = []

    def probe(_root: Path) -> None:
        try:
            lock = DbtLock(tmp_path, source="probe", operation="probe")
            lock.acquire(timeout=0)
            lock.release()
            states.append(False)
        except DbtLockError:
            states.append(True)

    with (
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=probe),
    ):
        run_local_prologue(tmp_path)
        run_local_prologue(tmp_path, lock_schemas=True)
    assert states == [False, True]


def test_load_project_env_keeps_existing_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WP32_KEEP", "real")
    monkeypatch.setenv("WP32_NEW", "x")
    monkeypatch.delenv("WP32_NEW")
    (tmp_path / ".env").write_text("WP32_KEEP=file\nWP32_NEW=from-file\n", encoding="utf-8")
    load_project_env(tmp_path)
    import os

    assert os.environ["WP32_KEEP"] == "real"
    assert os.environ["WP32_NEW"] == "from-file"
    monkeypatch.delenv("WP32_NEW")


def test_load_project_env_without_a_file_is_a_noop(tmp_path: Path) -> None:
    load_project_env(tmp_path)


def test_load_project_env_skip_ignores_named_keys_and_reports_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delenv("WP35_SKIPPED", raising=False)
    monkeypatch.delenv("WP35_TAKEN", raising=False)
    (tmp_path / ".env").write_text("WP35_SKIPPED=file\nWP35_TAKEN=file\n", encoding="utf-8")
    ignored = load_project_env(tmp_path, skip=frozenset({"WP35_SKIPPED", "WP35_ABSENT"}))
    assert ignored == ("WP35_SKIPPED",)
    assert "WP35_SKIPPED" not in os.environ
    assert os.environ["WP35_TAKEN"] == "file"
    monkeypatch.delenv("WP35_TAKEN")


def test_load_project_env_skip_leaves_a_shell_value_alone_and_does_not_report_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setenv("WP35_SKIPPED", "real")
    (tmp_path / ".env").write_text("WP35_SKIPPED=file\n", encoding="utf-8")
    assert load_project_env(tmp_path, skip=frozenset({"WP35_SKIPPED"})) == ()
    assert os.environ["WP35_SKIPPED"] == "real"


def test_load_project_env_without_skip_still_loads_every_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delenv("WP35_ANY", raising=False)
    (tmp_path / ".env").write_text("WP35_ANY=file\n", encoding="utf-8")
    assert load_project_env(tmp_path) == ()
    assert os.environ["WP35_ANY"] == "file"
    monkeypatch.delenv("WP35_ANY")


def test_load_project_env_raises_on_a_non_utf8_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"A=caf\xe9\n")
    with pytest.raises(UnicodeDecodeError):
        load_project_env(tmp_path)
