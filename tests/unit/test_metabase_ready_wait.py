"""tests/unit/test_metabase_ready_wait.py

Verify the read-only pending checks and the health-only Metabase wait used by
`dango start` and `dango serve` before credential migration and dashboard import.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.common.metabase_credential_migration import migration_pending
from dango.platform.common.startup import (
    metabase_startup_work_pending,
    wait_for_metabase_if_needed,
)

_WAIT = "dango.visualization.metabase.wait_for_metabase_ready"


def _write_metadata(root: Path, text: str) -> None:
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "metabase.yml").write_text(text, encoding="utf-8")


_LEGACY = "metabase_url: http://localhost:3000\nadmin:\n  email: a@b.co\n  password: secret\n"


@pytest.mark.unit
def test_migration_pending_true_with_legacy_password(tmp_path: Path) -> None:
    _write_metadata(tmp_path, _LEGACY)
    assert migration_pending(tmp_path) is True


@pytest.mark.unit
def test_migration_pending_false_without_password_file_or_on_error(tmp_path: Path) -> None:
    assert migration_pending(tmp_path) is False  # no file
    _write_metadata(tmp_path, "admin:\n  email: a@b.co\n")
    assert migration_pending(tmp_path) is False  # password-free
    _write_metadata(tmp_path, "admin: [unclosed\n  : :")
    assert migration_pending(tmp_path) is False  # malformed


@pytest.mark.unit
def test_work_pending_for_legacy_password(tmp_path: Path) -> None:
    _write_metadata(tmp_path, _LEGACY)
    assert metabase_startup_work_pending(tmp_path) is True


@pytest.mark.unit
def test_work_pending_for_dashboard_exports(tmp_path: Path) -> None:
    (tmp_path / "metabase").mkdir()
    (tmp_path / "dashboards").mkdir()
    assert metabase_startup_work_pending(tmp_path) is False  # empty dirs

    (tmp_path / "metabase" / "sub").mkdir()
    (tmp_path / "metabase" / "sub" / "x.yml").write_text("a: 1\n")
    assert metabase_startup_work_pending(tmp_path) is True

    (tmp_path / "metabase" / "sub" / "x.yml").unlink()
    (tmp_path / "dashboards" / "x.yml").write_text("a: 1\n")
    assert metabase_startup_work_pending(tmp_path) is True


@pytest.mark.unit
def test_no_pending_work_means_no_wait(tmp_path: Path) -> None:
    with patch(_WAIT) as wait:
        assert wait_for_metabase_if_needed(tmp_path) is None
    wait.assert_not_called()


@pytest.mark.unit
def test_wait_uses_metadata_url_and_120s_default(tmp_path: Path) -> None:
    _write_metadata(tmp_path, _LEGACY.replace("http://localhost:3000", "http://localhost:13060/"))
    with patch(_WAIT, return_value=True) as wait:
        wait_for_metabase_if_needed(tmp_path)
    wait.assert_called_once_with("http://localhost:13060", timeout=120)

    _write_metadata(tmp_path, "admin:\n  email: a@b.co\n  password: secret\n")
    with patch(_WAIT, return_value=True) as wait:
        wait_for_metabase_if_needed(tmp_path)
    wait.assert_called_once_with("http://localhost:3000", timeout=120)


@pytest.mark.unit
@pytest.mark.parametrize("ready", [True, False])
def test_wait_returns_true_and_false_from_helper(tmp_path: Path, ready: bool) -> None:
    _write_metadata(tmp_path, _LEGACY)
    with patch(_WAIT, return_value=ready):
        assert wait_for_metabase_if_needed(tmp_path) is ready


@pytest.mark.unit
def test_wait_makes_no_login_request(tmp_path: Path) -> None:
    _write_metadata(tmp_path, _LEGACY)
    boom = AssertionError("login/POST attempted during wait")
    healthy = MagicMock(status_code=200)
    with (
        patch("requests.post", side_effect=boom),
        patch("requests.Session.post", side_effect=boom),
    ):
        with patch(_WAIT, return_value=True):
            assert wait_for_metabase_if_needed(tmp_path) is True
        with patch("requests.Session.get", return_value=healthy) as get:
            assert wait_for_metabase_if_needed(tmp_path) is True
        assert get.called
