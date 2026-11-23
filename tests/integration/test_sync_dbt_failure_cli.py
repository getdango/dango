"""tests/integration/test_sync_dbt_failure_cli.py

`dango sync` end to end (real CLI, real run_sync, real dbt + DuckDB, no Docker): a dbt build that
skipped models is reported honestly and exits 1; a clean sync exits 0; failed checks that did not
stop the build are shown as a note.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.source import sync
from tests.integration.dbt_outcome_support import make_csv_project, write_orders_csv

pytestmark = pytest.mark.integration

CLEAN_CSV = "id,name\n1,a\n2,b\n3,c\n"
DUPLICATE_CSV = "id,name\n1,a\n2,b\n3,c\n3,dup\n"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DANGO_TELEMETRY", "false")
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)


def _sync(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Result, str]:
    monkeypatch.chdir(root)
    result = CliRunner().invoke(sync, ["--yes"], obj={"project_root": root})
    return result, " ".join(result.output.split())


def test_clean_sync_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV)
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "dbt models executed successfully" in text
    assert "data tests failed" not in text


def test_sync_that_skips_models_exits_one_and_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV)
    first, _ = _sync(root, monkeypatch)
    assert first.exit_code == 0, first.output

    write_orders_csv(root, DUPLICATE_CSV)  # a later load with a duplicate key
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 1, result.output
    assert "dbt transform failed" in text
    assert "dbt models executed successfully" not in text
    assert "Staging/marts tables were not created" not in text
    assert "stg_shop__shop" in text and "order_count" in text
    assert "source_unique_shop_shop_id" in text


def test_failed_check_that_skipped_nothing_is_shown_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV, failing_leaf_test=True)
    result, text = _sync(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "data tests failed" in text
    assert "accepted_values_order_count_n" in text
