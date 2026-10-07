"""tests/unit/test_serve_credential_messages.py

Verify dango serve tells the operator how to repair a lost Metabase admin credential.
Both commands appear verbatim, serve never repairs, and a rejected login check runs off-path.
"""

from __future__ import annotations

import io
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
) -> tuple[str, MagicMock, MagicMock]:
    """Run serve; return (output, Thread mock, requests.post mock). No real thread runs."""
    loader = (
        {"side_effect": credentials}
        if isinstance(credentials, Exception)
        else {"return_value": credentials}
    )
    with (
        patch(_REPAIR) as repair,
        patch(_LOADER, **loader),
        patch("requests.get", return_value=_response(200)),
        patch("requests.post", return_value=_response(200)) as post,
        patch("dango.cli.commands.serve.threading.Thread") as thread,
    ):
        result, _events = _run_cloud(root, migration)
    assert result.exit_code == 0, result.output
    repair.assert_not_called()
    return _ANSI_RE.sub("", result.output), thread, post


def _watch(
    root: Path, *, health: list[int | Exception], login: int = 401
) -> tuple[str, MagicMock, MagicMock, MagicMock]:
    """Run the background check body with a fake clock; return output and the HTTP mocks."""
    from dango.cli.commands.serve import _watch_for_rejected_credential

    def _get(*_a: object, **_k: object) -> MagicMock:
        item = health.pop(0) if health else 503
        if isinstance(item, Exception):
            raise item
        return _response(item)

    err = io.StringIO()
    with (
        patch(_LOADER, return_value=("a@b.co", "plain-secret-xyz")),
        patch("requests.get", side_effect=_get) as get,
        patch("requests.post", return_value=_response(login)) as post,
        patch("dango.cli.commands.serve.time.sleep") as sleep,
        patch("sys.stderr", err),
    ):
        _watch_for_rejected_credential(root)
    return err.getvalue(), get, post, sleep


@pytest.mark.unit
def test_serve_missing_credential_prints_both_commands(tmp_path: Path) -> None:
    _write_project(tmp_path)
    output, thread, post = _serve(tmp_path, _NOT_REQUIRED, credentials=None)
    _assert_actionable(output)
    post.assert_not_called()
    thread.assert_not_called()


@pytest.mark.unit
def test_serve_unreadable_credential_prints_both_commands(tmp_path: Path) -> None:
    _write_project(tmp_path)
    output, thread, post = _serve(tmp_path, _NOT_REQUIRED, credentials=RuntimeError("bad"))
    assert "unreadable" in output
    _assert_actionable(output)
    post.assert_not_called()
    thread.assert_not_called()


@pytest.mark.unit
def test_serve_with_credential_starts_one_daemon_thread_and_makes_no_login(
    tmp_path: Path,
) -> None:
    _write_project(tmp_path)
    output, thread, post = _serve(tmp_path, _NOT_REQUIRED, credentials=("a@b.co", "pw"))
    assert _HEADER not in output
    post.assert_not_called()
    thread.assert_called_once()
    assert thread.call_args.kwargs["daemon"] is True
    thread.return_value.start.assert_called_once()


@pytest.mark.unit
def test_serve_not_configured_starts_no_thread(tmp_path: Path) -> None:
    output, thread, post = _serve(tmp_path, _NOT_REQUIRED)
    assert _HEADER not in output
    thread.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
def test_watch_polls_health_until_ready_then_one_login_and_message(tmp_path: Path) -> None:
    _write_project(tmp_path)
    err, _get, post, sleep = _watch(tmp_path, health=[503, 503, ConnectionError("boot"), 200, 200])
    _assert_actionable(err)
    assert err.count(_HEADER) == 1
    assert post.call_count == 1
    assert sleep.call_count == 3
    assert "plain-secret-xyz" not in err


@pytest.mark.unit
def test_watch_never_healthy_makes_no_login_and_prints_nothing(tmp_path: Path) -> None:
    _write_project(tmp_path)
    err, get, post, sleep = _watch(tmp_path, health=[])
    assert err == ""
    post.assert_not_called()
    assert get.call_count == 36
    assert sleep.call_count == 36


@pytest.mark.unit
def test_watch_accepted_credential_prints_nothing(tmp_path: Path) -> None:
    _write_project(tmp_path)
    err, _get, post, _sleep = _watch(tmp_path, health=[200, 200], login=200)
    assert err == ""
    assert post.call_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason", ["credential_recovery_pending", "protected_store_unavailable", "login_unavailable"]
)
def test_serve_recovery_pending_message_has_cloud_command(tmp_path: Path, reason: str) -> None:
    _write_project(tmp_path)
    output, thread, post = _serve(
        tmp_path, {"status": "failed_non_destructive", "reason": reason}, credentials=None
    )
    _assert_actionable(output)
    # The migration already attempted its own login, so serve adds no second one.
    post.assert_not_called()
    thread.assert_not_called()
    assert output.count(_HEADER) == 1


@pytest.mark.unit
def test_serve_permanent_rejection_keeps_message_and_adds_commands(tmp_path: Path) -> None:
    output, _thread, _post = _serve(
        tmp_path,
        {"status": "failed_non_destructive", "reason": "current_credential_not_accepted"},
    )
    assert "could not sign in to its Metabase admin account" in output
    _assert_actionable(output)
