"""tests/unit/test_run_warned_tests_note.py

`dango run` says plainly what warned or failed: a note for warn-only checks (exit 0) and a hint
when every failure is a sample-generated staging `not_null` (exit code unchanged).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.transform import run
from tests.factories.scaffold_project import make_scaffold

MODEL = "stg_shop__orders"


def _plain(output: str) -> str:
    """Output without ANSI codes and with the console's line wrapping collapsed."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def _test(
    name: str,
    status: str,
    column: str | None = None,
    *,
    attached: str = MODEL,
    kind: str = "not_null",
) -> dict[str, Any]:
    """A dbt test node: its run_results entry plus its manifest node."""
    uid = f"test.proj.{name}.abc123"
    return {
        "uid": uid,
        "status": status,
        "manifest": {
            "resource_type": "test",
            "column_name": column,
            "attached_node": attached if "." in attached else f"model.proj.{attached}",
            "test_metadata": {"name": kind},
        },
    }


def _model(name: str, status: str) -> dict[str, Any]:
    return {"uid": f"model.proj.{name}", "status": status, "manifest": {"resource_type": "model"}}


def _invoke(
    tmp_path: Path,
    nodes: list[dict[str, Any]],
    *,
    returncode: int = 0,
    stale: bool = False,
    manifest: str = "ok",
) -> Result:
    """Run `dango run` with a fake dbt that writes run_results.json (older than the run if stale)."""
    target = tmp_path / "dbt" / "target"
    target.mkdir(parents=True, exist_ok=True)

    def write_results() -> None:
        results = [{"unique_id": n["uid"], "status": n["status"], "message": None} for n in nodes]
        path = target / "run_results.json"
        path.write_text(json.dumps({"results": results}), encoding="utf-8", newline="\n")
        nodes_json = json.dumps({"nodes": {n["uid"]: n["manifest"] for n in nodes}})
        if manifest == "corrupt":
            nodes_json = nodes_json[: len(nodes_json) // 2]
        if manifest != "missing":
            (target / "manifest.json").write_text(nodes_json, encoding="utf-8", newline="\n")
        # Pin the mtime: a coarse filesystem clock can stamp a just-written file slightly
        # before the build start that the CLI recorded.
        when = time.time() - 3600 if stale else time.time() + 60
        os.utime(path, (when, when))

    if stale:
        write_results()

    def fake_dbt(*_args: object, **_kwargs: object) -> SimpleNamespace:
        if not stale:
            write_results()
        return SimpleNamespace(returncode=returncode)

    fin = MagicMock(
        return_value={
            "model_status": "updated",
            "schema_sync": "updated",
            "metabase": "not_running",
            "metabase_schema_synced": False,
        }
    )
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("subprocess.run", side_effect=fake_dbt),
        patch("dango.utils.DbtLock"),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            return_value=False,
        ),
        patch("dango.transformation.build_finalize.finalize_dbt_build", fin),
    ):
        return CliRunner().invoke(run, [])


@pytest.fixture(autouse=True)
def _project_scaffold(tmp_path: Path) -> None:
    make_scaffold(tmp_path)


# -- the note for warned checks --------------------------------------------------------


@pytest.mark.unit
def test_warned_test_is_reported_and_exit_is_zero(tmp_path: Path) -> None:
    result = _invoke(
        tmp_path,
        [_test("not_null_stg_a__t_x", "warn", "x"), _test("unique_stg_a__t_id", "pass", "id")],
    )
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "the build succeeded. 1 data check(s) raised a warning" in out
    assert "not_null_stg_a__t_x" in out
    assert "unique_stg_a__t_id" not in out


@pytest.mark.unit
def test_note_does_not_claim_every_warning_was_inferred_by_dango(tmp_path: Path) -> None:
    out = _plain(_invoke(tmp_path, [_test("not_null_stg_a__t_x", "warn", "x")]).output)
    assert "Checks Dango inferred" not in out
    assert "warnings do not stop the run" in out
    assert "generated from a sample of your data are warn-only" in out


@pytest.mark.unit
def test_note_text_is_not_syntax_highlighted(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [_test("not_null_stg_a__t_x", "warn", "x")])
    assert "data check(s) raised" in result.output  # no ANSI codes inside the phrase


@pytest.mark.unit
def test_no_note_when_nothing_warned(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [_test("unique_stg_a__t_id", "pass", "id")])
    assert result.exit_code == 0, result.output
    assert "raised a warning" not in _plain(result.output)


@pytest.mark.unit
def test_stale_run_results_do_not_produce_a_note(tmp_path: Path) -> None:
    """A run_results.json from an earlier build (dbt wrote none this time) is ignored."""
    result = _invoke(tmp_path, [_test("not_null_stg_a__t_x", "warn", "x")], stale=True)
    assert result.exit_code == 0, result.output
    assert "raised a warning" not in _plain(result.output)


# -- the hint for sample-generated staging checks that failed --------------------------

HINT = "severity: warn"


@pytest.mark.unit
def test_hint_when_every_failure_is_a_staging_not_null_on_a_non_key_column(tmp_path: Path) -> None:
    result = _invoke(
        tmp_path,
        [
            _test(f"not_null_{MODEL}_customer_id", "fail", "customer_id"),
            _test(f"not_null_{MODEL}_email", "fail", "email"),
            _test(f"unique_{MODEL}_id", "pass", "id", kind="unique"),
        ],
        returncode=1,
    )
    out = _plain(result.output)
    assert result.exit_code == 1
    assert HINT in out
    assert "dbt/models/staging/" in out
    assert "generated from a sample of your data" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra",
    [
        _test(f"unique_{MODEL}_id", "fail", "id", kind="unique"),
        _test(f"not_null_{MODEL}_id", "fail", "id"),
        _test(f"not_null_{MODEL}_key", "fail", "key"),
        _test(f"unique_{MODEL}_customer_id", "fail", "customer_id", kind="unique"),
        _test(f"accepted_values_{MODEL}_status", "fail", "status", kind="accepted_values"),
        _test("not_null_stg_seed_x", "fail", "x", attached="seed.proj.stg_seed"),
        _test("not_null_stg_snap_x", "fail", "x", attached="snapshot.proj.stg_snap"),
        _test("not_null_order_count_n", "fail", "n", attached="order_count"),
        _test("source_not_null_shop_orders_status", "fail", "status", attached="orders"),
        _model("order_count", "error"),
    ],
    ids=[
        "unique-key",
        "key-id",
        "key-key",
        "unique-ordinary",
        "accepted-values",
        "seed-test",
        "snapshot-test",
        "mart-test",
        "source-test",
        "model-error",
    ],
)
def test_no_hint_when_any_other_failure_exists(tmp_path: Path, extra: dict[str, Any]) -> None:
    result = _invoke(
        tmp_path,
        [_test(f"not_null_{MODEL}_customer_id", "fail", "customer_id"), extra],
        returncode=1,
    )
    assert result.exit_code == 1
    assert HINT not in _plain(result.output)


@pytest.mark.unit
def test_no_hint_for_a_key_column_failure_alone(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [_test(f"not_null_{MODEL}_id", "fail", "id")], returncode=1)
    assert result.exit_code == 1
    assert HINT not in _plain(result.output)


@pytest.mark.unit
def test_no_hint_from_stale_run_results(tmp_path: Path) -> None:
    result = _invoke(
        tmp_path,
        [_test(f"not_null_{MODEL}_customer_id", "fail", "customer_id")],
        returncode=1,
        stale=True,
    )
    assert result.exit_code == 1
    assert HINT not in _plain(result.output)


@pytest.mark.unit
def test_no_hint_when_the_build_has_no_results_at_all(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [], returncode=1)
    assert result.exit_code == 1
    assert HINT not in _plain(result.output)


@pytest.mark.unit
@pytest.mark.parametrize("manifest", ["missing", "corrupt"])
def test_no_hint_when_the_manifest_cannot_be_read(tmp_path: Path, manifest: str) -> None:
    result = _invoke(
        tmp_path,
        [_test(f"not_null_{MODEL}_customer_id", "fail", "customer_id")],
        returncode=1,
        manifest=manifest,
    )
    assert result.exit_code == 1
    assert HINT not in _plain(result.output)
