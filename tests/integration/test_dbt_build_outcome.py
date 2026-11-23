"""tests/integration/test_dbt_build_outcome.py

`run_dbt_models` decides success from dbt's run_results.json (real dbt + DuckDB): skipped or
errored models fail the build, test-only failures keep it a success but stay visible, and a
non-zero exit without usable results fails closed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from dango.transformation import run_dbt_models
from tests.integration.dbt_outcome_support import VARIANTS, make_matrix_project

pytestmark = pytest.mark.integration

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
            if age_seconds:
                old = time.time() - age_seconds
                os.utime(path, (old, old))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    with patch("dango.transformation.subprocess.run", side_effect=fake_run):
        return run_dbt_models(root)


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    (tmp_path / "dbt").mkdir()
    return tmp_path


def test_clean_build_succeeds_with_no_notes(outcomes: dict[str, Outcome]) -> None:
    assert outcomes["clean"].ok is True
    assert outcomes["clean"].notes == []


def test_failing_test_with_nothing_skipped_is_success_and_names_the_test(
    outcomes: dict[str, Outcome],
) -> None:
    out = outcomes["test_only"]
    assert out.ok is True
    assert any("data tests failed" in n and "accepted_values_order_count_n" in n for n in out.notes)
    assert not any("skipped" in n or "errors" in n for n in out.notes)


def test_warn_only_build_is_success_without_a_failure_note(outcomes: dict[str, Outcome]) -> None:
    assert outcomes["warn_only"].ok is True
    assert outcomes["warn_only"].notes == []


def test_model_sql_error_fails_and_names_the_model(outcomes: dict[str, Outcome]) -> None:
    out = outcomes["model_error"]
    assert out.ok is False
    assert any("models with errors" in n and "broken_mart" in n for n in out.notes)


def test_skipped_models_fail_and_name_models_and_the_failing_test(
    outcomes: dict[str, Outcome],
) -> None:
    out = outcomes["skipped"]
    assert out.ok is False
    assert any("skipped" in n and "order_count" in n for n in out.notes)
    assert any("data tests failed" in n and "unique_stg_orders_id" in n for n in out.notes)
    assert "dbt build failed" in out.output


def test_genuine_results_with_skipped_nodes_fail_even_with_old_success_text(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, _ = _fake_dbt_run(bare_project, results=outcomes["skipped"].results)
    assert ok is False


def test_genuine_test_only_results_keep_the_build_a_success(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, output = _fake_dbt_run(bare_project, results=outcomes["test_only"].results)
    assert ok is True
    assert "data tests failed" in output


def test_nonzero_exit_without_results_fails_despite_completed_with_test_text(
    bare_project: Path,
) -> None:
    ok, output = _fake_dbt_run(bare_project, results=None)
    assert ok is False
    assert "Full output:" in output


def test_nonzero_exit_with_stale_results_fails_closed(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, _ = _fake_dbt_run(bare_project, results=outcomes["test_only"].results, age_seconds=3600)
    assert ok is False


def test_nonzero_exit_with_unparseable_results_fails_closed(bare_project: Path) -> None:
    ok, _ = _fake_dbt_run(bare_project, results=b'{"results": [ not json')
    assert ok is False


def test_nonzero_exit_that_no_node_explains_fails_closed(
    outcomes: dict[str, Outcome], bare_project: Path
) -> None:
    ok, _ = _fake_dbt_run(bare_project, results=outcomes["clean"].results)
    assert ok is False
