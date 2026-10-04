"""tests/unit/test_start_metabase_admin_repair_wiring.py

Verify `dango start` runs the automatic Metabase admin repair only after a permanent
credential rejection, prints the right messages, and never lets a repair error stop startup.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.test_metabase_credential_lifecycle_commands import _run_cloud, _run_local

_REPAIR = "dango.platform.common.metabase_admin_repair.repair_admin_credential"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REJECTED = {"status": "failed_non_destructive", "reason": "current_credential_not_accepted"}


def _plain(output: str) -> str:
    return re.sub(r"\s+", " ", _ANSI_RE.sub("", output))


@pytest.mark.unit
def test_repair_called_for_permanent_rejection_and_success_prints_restored(
    tmp_path: Path,
) -> None:
    with patch(_REPAIR, return_value={"status": "repaired"}) as repair:
        result, events = _run_local(tmp_path, _REJECTED)

    text = _plain(result.output)
    assert result.exit_code == 0, result.output
    repair.assert_called_once_with(tmp_path)
    assert "Metabase admin access restored." in text
    assert "could not sign in to its Metabase admin account" not in text
    assert events == ["docker", "gitignore", "completion", "setup"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "migration_result",
    [
        {"status": "secure_rotated"},
        {"status": "not_required"},
        {"status": "failed_non_destructive", "reason": "login_unavailable"},
        {"status": "failed_non_destructive", "reason": "protected_store_unavailable"},
        {"status": "failed_non_destructive"},
    ],
)
def test_repair_not_called_for_other_outcomes(
    tmp_path: Path, migration_result: dict[str, object]
) -> None:
    with patch(_REPAIR) as repair:
        result, _events = _run_local(tmp_path, migration_result)

    assert result.exit_code == 0, result.output
    repair.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "failed", "reason": "reset_cli_failed"},
        {"status": "skipped", "reason": "metabase_unreachable"},
    ],
)
def test_failed_or_skipped_repair_prints_permanent_message_and_hint(
    tmp_path: Path, outcome: dict[str, object]
) -> None:
    with patch(_REPAIR, return_value=outcome):
        result, events = _run_local(tmp_path, _REJECTED)

    text = _plain(result.output)
    assert result.exit_code == 0, result.output
    assert "could not sign in to its Metabase admin account" in text
    assert 'Run "dango metabase repair-admin" to try again.' in text
    assert "Metabase admin access restored." not in text
    assert events[-1] == "setup"


@pytest.mark.unit
def test_repair_exception_is_swallowed_and_start_continues_to_setup(tmp_path: Path) -> None:
    with patch(_REPAIR, side_effect=RuntimeError("boom")):
        result, events = _run_local(tmp_path, _REJECTED)

    assert result.exit_code == 0, result.output
    assert events == ["docker", "gitignore", "completion", "setup"]
    assert "Metabase credential migration is incomplete" in _plain(result.output)


@pytest.mark.unit
def test_serve_never_calls_repair(tmp_path: Path) -> None:
    with patch(_REPAIR) as repair:
        result, _events = _run_cloud(tmp_path, _REJECTED)

    assert result.exit_code == 0, result.output
    repair.assert_not_called()
    assert "could not sign in to its Metabase admin account" in _plain(result.output)
