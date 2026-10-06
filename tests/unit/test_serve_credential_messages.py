"""tests/unit/test_serve_credential_messages.py

Verify dango serve tells the operator how to repair a lost Metabase admin credential.
Both repair commands appear verbatim, serve never repairs, and it makes at most one login.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tests.unit.test_metabase_credential_lifecycle_commands import _run_cloud

_REPAIR = "dango.platform.common.metabase_admin_repair.repair_admin_credential"
_LOADER = "dango.security.metabase_config.load_metabase_admin_credentials"
_NOT_REQUIRED = {"status": "not_required"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SERVER_COMMAND = (
    "cd /srv/dango/project && sudo -u dango -H env DANGO_CLOUD_MODE=true "
    "/srv/dango/venv/bin/dango metabase repair-admin"
)
_LAPTOP_COMMAND = "dango remote metabase-repair-admin"
_HEADER = "Metabase admin access needs repair. On this server run:"
_ROOT_NOTE = "(Running it as root would leave root-owned files in the credential store.)"


def _write_project(root: Path) -> None:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    (root / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": "http://localhost:3000", "admin": {"email": "a@b.co"}}),
        encoding="utf-8",
    )


def _response(status: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    return response


def _assert_actionable(output: str) -> None:
    assert _HEADER in output
    assert _SERVER_COMMAND in output
    assert _LAPTOP_COMMAND in output
    assert _ROOT_NOTE in output
    assert "will retry on the next start" not in output


def _serve(
    root: Path,
    migration: dict[str, object],
    *,
    credentials: object = None,
    login: int = 200,
) -> tuple[str, MagicMock, MagicMock]:
    loader = (
        {"side_effect": credentials}
        if isinstance(credentials, Exception)
        else {"return_value": credentials}
    )
    with (
        patch(_REPAIR) as repair,
        patch(_LOADER, **loader),
        patch("requests.get", return_value=_response(200)),
        patch("requests.post", return_value=_response(login)) as post,
    ):
        result, _events = _run_cloud(root, migration)
    assert result.exit_code == 0, result.output
    repair.assert_not_called()
    return _ANSI_RE.sub("", result.output), repair, post


@pytest.mark.unit
def test_serve_missing_credential_prints_both_commands(tmp_path: Path) -> None:
    _write_project(tmp_path)
    output, _repair, post = _serve(tmp_path, _NOT_REQUIRED, credentials=None)
    _assert_actionable(output)
    post.assert_not_called()


@pytest.mark.unit
def test_serve_unreadable_credential_prints_both_commands(tmp_path: Path) -> None:
    _write_project(tmp_path)
    output, _repair, post = _serve(tmp_path, _NOT_REQUIRED, credentials=RuntimeError("bad"))
    assert "unreadable" in output
    _assert_actionable(output)
    post.assert_not_called()


@pytest.mark.unit
def test_serve_rejected_credential_prints_both_commands_after_one_login(tmp_path: Path) -> None:
    _write_project(tmp_path)
    secret = "plain-secret-xyz"
    output, _repair, post = _serve(
        tmp_path, _NOT_REQUIRED, credentials=("a@b.co", secret), login=401
    )
    _assert_actionable(output)
    assert post.call_count == 1
    assert secret not in output


@pytest.mark.unit
def test_serve_is_silent_when_credential_works_or_not_configured(tmp_path: Path) -> None:
    output, _repair, post = _serve(tmp_path, _NOT_REQUIRED)
    assert _HEADER not in output
    post.assert_not_called()

    _write_project(tmp_path)
    output, _repair, post = _serve(tmp_path, _NOT_REQUIRED, credentials=("a@b.co", "pw"))
    assert _HEADER not in output
    assert post.call_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason", ["credential_recovery_pending", "protected_store_unavailable", "login_unavailable"]
)
def test_serve_recovery_pending_message_has_cloud_command(tmp_path: Path, reason: str) -> None:
    _write_project(tmp_path)
    output, _repair, post = _serve(
        tmp_path, {"status": "failed_non_destructive", "reason": reason}, credentials=None
    )
    _assert_actionable(output)
    # The migration already attempted its own login, so serve adds no second one.
    post.assert_not_called()
    assert output.count(_HEADER) == 1


@pytest.mark.unit
def test_serve_permanent_rejection_keeps_message_and_adds_commands(tmp_path: Path) -> None:
    output, _repair, _post = _serve(
        tmp_path,
        {"status": "failed_non_destructive", "reason": "current_credential_not_accepted"},
    )
    assert "could not sign in to its Metabase admin account" in output
    _assert_actionable(output)
