"""tests/unit/test_coalesced_dbt_reason.py

The scheduler's coalesced dbt run records WHY a failed dbt run failed in the activity log (one
redacted, cut line) and in the log fields; the success and lock-timeout messages are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.exceptions import DbtLockError
from dango.platform.scheduling import jobs

pytestmark = pytest.mark.unit

SECRET = "hunter2hunter2"
EXPLAINED = (
    "dbt build failed\n\nPossible causes:\n  - A test failed\n\n"
    "dango: models skipped because something they depend on failed (2): stg_shop, order_count\n"
    "dango: tests failed (1): unique_stg_shop_id\n\nFull output:\n"
    "05:12:16  Running with dbt=1.11.14\n05:12:17  Done. PASS=0 WARN=0 ERROR=1 SKIP=5\n"
)
UNEXPLAINED = (
    "dbt build failed\n\nPossible causes:\n  - SQL syntax error\n\n"
    "dango: the dbt build exited with an error that its run results do not explain "
    "(they are missing, stale, unreadable or show no failed node), so the build is treated as "
    "failed.\n\nFull output:\n05:12:16  Running with dbt=1.11.14\n"
    "05:12:17  Encountered an error:\nCompilation Error\n"
    "  Model 'model.proj.bad_ref' (models/marts/bad_ref.sql) depends on a node named "
    "'no_such_model' which was not found\n"
)
CAUSE = "no_such_model"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(jobs, "_get_coalesce_seconds", lambda _root: 0)
    monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection",
        lambda *_a, **_k: (False, None, None),
    )
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    jobs._add_pending_dbt_source(tmp_path, "shop")
    return tmp_path


def _activity(root: Path) -> list[dict[str, str]]:
    path = root / ".dango" / "logs" / "activity.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run(root: Path, result: tuple[bool, str]) -> bool:
    with (
        patch("dango.transformation.run_dbt_models", return_value=result),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            return_value=False,
        ),
    ):
        return jobs._run_coalesced_dbt(root)


def test_failed_run_records_the_reason_in_the_activity_log(root: Path) -> None:
    assert _run(root, (False, EXPLAINED)) is False
    last = _activity(root)[-1]
    assert last["level"] == "error"
    assert last["message"].startswith("Coalesced dbt failed for sources: shop.")
    assert "Tests failed (1): unique_stg_shop_id" in last["message"]
    assert "stg_shop, order_count" in last["message"]
    assert "\n" not in last["message"]
    assert "Running with dbt" not in last["message"]


def test_failed_run_without_a_reason_names_the_first_error_line(root: Path) -> None:
    _run(root, (False, UNEXPLAINED))
    message = _activity(root)[-1]["message"]
    assert "Compilation Error: Model 'model.proj.bad_ref'" in message
    assert CAUSE in message
    assert "\n" not in message


def test_the_reason_is_redacted_before_it_is_cut(root: Path) -> None:
    filler = "x" * 230
    output = f"05:12:17  Database Error in model m: {filler} password={SECRET}\n"
    _run(root, (False, output))
    message = _activity(root)[-1]["message"]
    assert "Database Error in model m" in message
    assert SECRET not in message
    assert len(message) < 600


def test_the_reason_is_cut_to_a_short_single_line(root: Path) -> None:
    names = ", ".join(f"model_{i}" for i in range(400))
    output = f"dango: failed models, seeds, snapshots or hooks (400): {names}\n"
    _run(root, (False, output))
    message = _activity(root)[-1]["message"]
    assert "model_0" in message
    assert "\n" not in message
    assert len(message) < 600


def test_the_log_fields_carry_the_reason(root: Path) -> None:
    with patch.object(jobs.logger, "error") as log:
        _run(root, (False, UNEXPLAINED))
    fields = json.dumps(log.call_args.kwargs, default=str)
    assert log.call_args.args[0] == "coalesced_dbt_run_failed"
    assert CAUSE in fields
    assert "run results do not explain" not in fields


def test_the_success_message_is_unchanged(root: Path) -> None:
    assert _run(root, (True, "all good")) is True
    messages = [e["message"] for e in _activity(root)]
    assert "Coalesced dbt completed for sources: shop" in messages


def test_the_lock_timeout_message_is_unchanged(root: Path) -> None:
    queue = "Sync queue timeout. Another sync is still running."
    with (
        patch("dango.transformation.run_dbt_models", side_effect=DbtLockError(queue)),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            return_value=False,
        ),
    ):
        assert jobs._run_coalesced_dbt(root) is False
    last = _activity(root)[-1]
    assert last["message"] == f"Coalesced dbt failed for sources: shop. {queue}"


def test_a_project_env_value_is_redacted_in_the_activity_log_and_log_fields(root: Path) -> None:
    secret = "plainsecretvalue987"
    (root / ".env").write_text(f"SHOP_API_KEY={secret}\n", encoding="utf-8", newline="\n")
    output = UNEXPLAINED.replace("a node named", f"a node named {secret} and")
    with patch.object(jobs.logger, "error") as log:
        _run(root, (False, output))
    message = _activity(root)[-1]["message"]
    assert CAUSE in message  # the cause is there, the bare project value is not
    assert secret not in message
    assert secret not in json.dumps(log.call_args.kwargs, default=str)
