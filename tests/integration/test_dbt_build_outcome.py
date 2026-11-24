"""tests/integration/test_dbt_build_outcome.py

`run_dbt_models` decides success from dbt's run_results.json (real dbt + DuckDB): skipped or
errored models fail the build, test-only failures keep it a success but stay visible, and a
non-zero exit without usable results fails closed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.transformation import build_outcome, run_dbt_models
from tests.integration.dbt_outcome_support import VARIANTS, make_matrix_project

pytestmark = pytest.mark.integration

# A results file the fake dbt "wrote during the build" is stamped this far after the real clock.
_FRESH_MARGIN_SECONDS = 5

COMPLETED_TEXT = (
    "Completed with 1 error, 0 partial successes, and 0 warnings:\n"
    "Failure in test not_null_x (models/schema.yml)\n"
)


@dataclass
class Outcome:
    ok: bool
    output: str
    results: bytes

    @property
    def notes(self) -> list[str]:
        return [ln for ln in self.output.splitlines() if ln.startswith("dango: ")]


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Outcome]]:
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    mp.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "False")
    mp.setenv("DO_NOT_TRACK", "1")
    found: dict[str, Outcome] = {}
    for variant in VARIANTS:
        root = make_matrix_project(tmp_path_factory.mktemp(variant) / "proj", variant)
        ok, output = run_dbt_models(root)
        results = (root / "dbt" / "target" / "run_results.json").read_bytes()
        found[variant] = Outcome(ok, output, results)
    yield found
    mp.undo()


def _fake_dbt_run(
    root: Path,
    *,
    results: bytes | None,
    age_seconds: float = 0,
    returncode: int = 1,
    stdout: str = COMPLETED_TEXT,
) -> tuple[bool, str]:
    """run_dbt_models with a dbt that exits `returncode` after writing `results` (or nothing)."""

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> SimpleNamespace:
        if results is not None:
            path = root / "dbt" / "target" / "run_results.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(results)
            # Stamp the mtime explicitly so freshness never depends on the file-system clock
            # granularity: unambiguously after the build started, or `age_seconds` before it.
            stamp = (
                time.time() - age_seconds if age_seconds else time.time() + _FRESH_MARGIN_SECONDS
            )
            os.utime(path, (stamp, stamp))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    with patch("dango.transformation.subprocess.run", side_effect=fake_run):
        return run_dbt_models(root)


def _fake_dbt_run_with_verdict(
    root: Path, *, results: bytes | None, **kwargs: Any
) -> tuple[bool, str, build_outcome.BuildVerdict]:
    """`_fake_dbt_run` plus the verdict `run_dbt_models` reached from the results file."""
    verdicts: list[build_outcome.BuildVerdict] = []
    real_assess = build_outcome.assess_failed_build

    def spy(project_root: Path, *, since: float) -> build_outcome.BuildVerdict:
        verdicts.append(real_assess(project_root, since=since))
        return verdicts[-1]

    with patch.object(build_outcome, "assess_failed_build", side_effect=spy):
        ok, output = _fake_dbt_run(root, results=results, **kwargs)
    assert len(verdicts) == 1, "a non-zero exit must reach the results verdict exactly once"
    return ok, output, verdicts[0]


@contextmanager
def _build_clock_ahead(seconds: float = 1.0) -> Iterator[None]:
    """The build-start stamp of `run_dbt_models` runs `seconds` ahead of the file-system clock.

    That is the worst case of a coarse file-system timestamp: a results file written right
    after the build started carries an mtime older than the stamp. Only `run_dbt_models`
    sees the shifted clock; the test's own `time` is untouched.
    """
    real_time = time.time
    with patch("dango.transformation.time", SimpleNamespace(time=lambda: real_time() + seconds)):
        yield


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    (tmp_path / "dbt").mkdir()
    return tmp_path


def _notes(output: str) -> list[str]:
    return [ln for ln in output.splitlines() if ln.startswith("dango: ")]


def test_clean_build_succeeds_with_no_notes(outcomes: dict[str, Outcome]) -> None:
    assert outcomes["clean"].ok is True
    assert outcomes["clean"].notes == []


def test_failing_test_with_nothing_skipped_is_success_and_names_the_test(
    outcomes: dict[str, Outcome],
) -> None:
    out = outcomes["test_only"]
    assert out.ok is True
    assert any("tests failed" in n and "accepted_values_order_count_n" in n for n in out.notes)
    assert not any("skipped" in n or "failed models" in n for n in out.notes)
    assert not any("__98" in n for n in out.notes)  # the warn-only test is not a failure
    assert any("(1)" in n for n in out.notes)


def test_warn_only_build_is_success_without_a_failure_note(outcomes: dict[str, Outcome]) -> None:
    assert outcomes["warn_only"].ok is True
    assert outcomes["warn_only"].notes == []


def test_model_sql_error_fails_and_names_the_model(outcomes: dict[str, Outcome]) -> None:
    out = outcomes["model_error"]
    assert out.ok is False
    assert any(
        "failed models, seeds, snapshots or hooks" in n and "broken_mart" in n for n in out.notes
    )
    # the cause is the failed node, not the DuckDB lock text that matched every dbt-duckdb run
    assert "write lock" not in out.output
    assert "Possible causes" in out.output and "named below failed" in out.output


def test_skipped_models_fail_and_name_models_and_the_failing_test(
    outcomes: dict[str, Outcome],
) -> None:
    out = outcomes["skipped"]
    assert out.ok is False
    assert any("skipped" in n and "order_count" in n for n in out.notes)
    assert any("tests failed" in n and "unique_stg_orders_id" in n for n in out.notes)
    assert "dbt build failed" in out.output


def test_failed_hook_beside_a_failing_leaf_test_fails_the_build(
    outcomes: dict[str, Outcome],
) -> None:
    out = outcomes["hook_and_test"]
    assert out.ok is False
    assert any("failed models, seeds, snapshots or hooks" in n for n in out.notes)
    assert not any("skipped" in n for n in out.notes)  # nothing skipped: the hook alone fails it


def test_test_whose_own_sql_errors_is_a_success_with_a_note(outcomes: dict[str, Outcome]) -> None:
    out = outcomes["test_error"]
    assert out.ok is True
    assert any("tests failed" in n and "leaf_test_with_bad_sql" in n for n in out.notes)


def test_genuine_results_with_skipped_nodes_fail_even_with_old_success_text(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(
        bare_project, results=outcomes["skipped"].results
    )
    assert ok is False
    # the verdict comes from the results (not from an unreadable or stale file)
    assert verdict.usable is True
    assert verdict.skipped_models is True and "order_count" in verdict.skipped
    assert "unique_stg_orders_id" in verdict.tests_failed
    assert build_outcome.UNEXPLAINED_NOTE not in output
    notes = [n for n in output.splitlines() if n.startswith("dango: ")]
    assert any("skipped" in n and "order_count" in n for n in notes)
    assert any("tests failed" in n and "unique_stg_orders_id" in n for n in notes)


def test_genuine_test_only_results_keep_the_build_a_success(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(
        bare_project, results=outcomes["test_only"].results
    )
    assert ok is True
    assert verdict.transformed is True
    assert any("tests failed" in n and "accepted_values_order_count_n" in n for n in _notes(output))
    assert build_outcome.UNEXPLAINED_NOTE not in output


def test_nonzero_exit_without_results_fails_despite_completed_with_test_text(
    bare_project: Path,
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(bare_project, results=None)
    assert ok is False
    assert verdict.usable is False
    assert "Full output:" in output
    assert build_outcome.UNEXPLAINED_NOTE in output


def test_nonzero_exit_with_stale_results_fails_closed(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(
        bare_project, results=outcomes["test_only"].results, age_seconds=3600
    )
    assert ok is False
    assert verdict.usable is False  # the file exists and parses; only its age rules it out
    assert build_outcome.UNEXPLAINED_NOTE in output


def test_nonzero_exit_with_unparseable_results_fails_closed(bare_project: Path) -> None:
    with patch("dango.transformation.build_finalize.logger") as log:
        ok, output, verdict = _fake_dbt_run_with_verdict(
            bare_project, results=b'{"results": [ not json'
        )
    assert ok is False
    assert verdict.usable is False
    # the file was read and rejected as unparseable (not skipped as stale)
    assert [c.args[0] for c in log.warning.call_args_list] == ["run_results_summary_failed"]
    assert build_outcome.UNEXPLAINED_NOTE in output


def test_nonzero_exit_that_no_node_explains_fails_closed(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(
        bare_project, results=outcomes["clean"].results
    )
    assert ok is False
    assert verdict.usable is True  # the results were read: they show no failed node
    assert _notes(output) == [build_outcome.UNEXPLAINED_NOTE]


def test_a_held_warehouse_lock_still_gets_the_lock_advice(tmp_path: Path) -> None:
    """A real DuckDB lock conflict (this process holds the file open) keeps the lock cause."""
    import duckdb

    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(tmp_path))
    mp.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "False")
    mp.setenv("DO_NOT_TRACK", "1")
    try:
        root = make_matrix_project(tmp_path / "proj", "clean")
        holder = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
        try:
            ok, output = run_dbt_models(root)
        finally:
            holder.close()
    finally:
        mp.undo()
    assert ok is False
    assert "lock" in output.lower()
    assert "DuckDB write lock held by another process" in output


# Variants whose real dbt run exits non-zero; replaying their genuine results through the fake
# dbt must reach the same verdict and the same notes as the real run did.
_FAILING_VARIANTS = ("test_only", "model_error", "skipped", "hook_and_test", "test_error")


@pytest.mark.parametrize("variant", _FAILING_VARIANTS)
def test_genuine_results_get_their_own_verdict_when_the_build_clock_runs_ahead(
    outcomes: dict[str, Outcome], bare_project: Path, variant: str
) -> None:
    """A fresh results file is never judged stale because of file-system timestamp granularity."""
    real = outcomes[variant]
    with _build_clock_ahead():
        ok, output, verdict = _fake_dbt_run_with_verdict(bare_project, results=real.results)
    assert verdict.usable is True
    assert ok is real.ok
    assert _notes(output) == real.notes
    assert build_outcome.UNEXPLAINED_NOTE not in output


def test_a_build_that_no_node_explains_is_judged_from_its_results_when_the_clock_runs_ahead(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    with _build_clock_ahead():
        ok, _, verdict = _fake_dbt_run_with_verdict(bare_project, results=outcomes["clean"].results)
    assert ok is False
    assert verdict.usable is True  # the results were read: they show no failed node


def test_stale_results_are_not_usable_even_when_the_clock_does_not_run_ahead(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output, verdict = _fake_dbt_run_with_verdict(
        bare_project, results=outcomes["test_only"].results, age_seconds=3600
    )
    assert ok is False
    assert verdict.usable is False
    assert build_outcome.UNEXPLAINED_NOTE in output
