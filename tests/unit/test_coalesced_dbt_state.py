"""tests/unit/test_coalesced_dbt_state.py

The scheduler's coalesced dbt run: it holds the dbt lock around dbt, and the pending sources it
consumed are put back (merged with any added meanwhile) whenever the run does not succeed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import DbtLockError
from dango.platform.scheduling import jobs
from dango.utils.dbt_lock import DbtLock

pytestmark = pytest.mark.unit

QUEUE_MESSAGE = "Sync queue timeout. Another sync is still running."
_STOP = "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes"
_START = "dango.platform.common.metabase_lifecycle.start_metabase_after_writes"


def _pending(root: Path) -> list[str]:
    return json.loads((root / ".dango" / "state" / "pending_dbt_sources.json").read_text("utf-8"))


def _lock_is_free(root: Path) -> bool:
    probe = DbtLock(root, source="probe", operation="probe")
    try:
        probe.acquire(timeout=0)
    except DbtLockError:
        return False
    probe.release()
    return True


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
    jobs._add_pending_dbt_source(tmp_path, "crm")
    return tmp_path


def _run(root: Path, dbt: Callable[..., tuple[bool, str]]) -> bool:
    with patch("dango.transformation.run_dbt_models", side_effect=dbt):
        return jobs._run_coalesced_dbt(root)


def test_success_clears_the_pending_sources(root: Path) -> None:
    assert _run(root, lambda *_a, **_k: (True, "ok")) is True
    assert _pending(root) == []


def test_source_added_during_a_successful_run_stays_pending(root: Path) -> None:
    def dbt(*_a: object, **_k: object) -> tuple[bool, str]:
        jobs._add_pending_dbt_source(root, "late")
        return (True, "ok")

    assert _run(root, dbt) is True
    assert _pending(root) == ["late"]


def test_failed_dbt_puts_the_sources_back(root: Path) -> None:
    assert _run(root, lambda *_a, **_k: (False, "boom")) is False
    assert _pending(root) == ["shop", "crm"]


def test_exception_puts_the_sources_back_and_propagates(root: Path) -> None:
    with pytest.raises(RuntimeError, match="kaput"):
        _run(root, MagicMock(side_effect=RuntimeError("kaput")))
    assert _pending(root) == ["shop", "crm"]


def test_lock_timeout_fails_with_the_queue_message_and_puts_the_sources_back(
    root: Path,
) -> None:
    dbt = MagicMock(return_value=(True, "ok"))
    with patch.object(DbtLock, "acquire", side_effect=DbtLockError(QUEUE_MESSAGE)):
        assert _run(root, dbt) is False

    dbt.assert_not_called()
    assert _pending(root) == ["shop", "crm"]
    log = (root / ".dango" / "logs" / "activity.jsonl").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in log.splitlines()]
    assert any(e["level"] == "error" and QUEUE_MESSAGE in e["message"] for e in entries)


def test_failed_run_merges_with_sources_added_meanwhile(root: Path) -> None:
    def dbt(*_a: object, **_k: object) -> tuple[bool, str]:
        jobs._add_pending_dbt_source(root, "late")
        jobs._add_pending_dbt_source(root, "shop")  # also consumed: must not be duplicated
        return (False, "boom")

    assert _run(root, dbt) is False
    assert _pending(root) == ["shop", "crm", "late"]
    path = root / ".dango" / "state" / "pending_dbt_sources.json"
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), list)


def test_the_lock_is_held_while_dbt_runs(root: Path) -> None:
    seen: list[bool] = []

    def dbt(*_a: object, **_k: object) -> tuple[bool, str]:
        seen.append(_lock_is_free(root))
        return (True, "ok")

    assert _run(root, dbt) is True
    assert seen == [False]


@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
def test_the_lock_is_released_before_metabase_restarts(root: Path, outcome: str) -> None:
    free_at_restart: list[bool] = []

    def dbt(*_a: object, **_k: object) -> tuple[bool, str]:
        if outcome == "exception":
            raise RuntimeError("kaput")
        return (outcome == "success", "")

    def restart(*_a: object, **_k: object) -> bool:
        free_at_restart.append(_lock_is_free(root))
        return True

    with patch(_STOP, return_value=True), patch(_START, side_effect=restart):
        if outcome == "exception":
            with pytest.raises(RuntimeError):
                _run(root, dbt)
        else:
            _run(root, dbt)

    assert free_at_restart == [True]


def test_metabase_is_stopped_before_dbt_and_restarted_after(root: Path) -> None:
    order: list[str] = []

    def dbt(*_a: object, **_k: object) -> tuple[bool, str]:
        order.append("dbt")
        return (False, "boom")

    with (
        patch(_STOP, side_effect=lambda *_a, **_k: order.append("stop") or True),
        patch(_START, side_effect=lambda *_a, **_k: order.append("start") or True),
    ):
        _run(root, dbt)

    assert order == ["stop", "dbt", "start"]


def test_metabase_is_not_restarted_when_it_was_not_stopped(root: Path) -> None:
    with patch(_STOP, return_value=False), patch(_START) as start:
        _run(root, lambda *_a, **_k: (True, "ok"))
    start.assert_not_called()


def test_docs_are_generated_under_the_lock_after_a_successful_run(root: Path) -> None:
    held_during_docs: list[bool] = []

    def docs(*_a: object, **_k: object) -> tuple[bool, str]:
        held_during_docs.append(not _lock_is_free(root))
        return (True, "")

    with patch("dango.transformation.generate_dbt_docs", side_effect=docs):
        assert _run(root, lambda *_a, **_k: (True, "ok")) is True

    assert held_during_docs == [True]


def test_no_docs_after_a_failed_run(root: Path) -> None:
    with patch("dango.transformation.generate_dbt_docs") as docs:
        _run(root, lambda *_a, **_k: (False, "boom"))
    docs.assert_not_called()
