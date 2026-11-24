"""tests/unit/test_start_metabase_setup_failure_message.py

`dango start` must not claim "DuckDB connected" when Metabase setup stopped before that step, must
print setup error lines escaped and redacted, and must say that running `dango start` again is safe.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from dango.cli.commands.platform import start
from tests.unit.test_metabase_credential_lifecycle_commands import (
    _CREDENTIAL_MIGRATION,
    _STARTUP,
    _config,
    _response,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TIMEOUT_ERROR = "Setup error: HTTPConnectionPool(host='localhost', port=3000): Read timed out. (read timeout=10)"
_CLAIM = "DuckDB connected"
_RERUN = "running dango start again is safe and retries the setup"
_MANUAL_URL = "http://localhost:3000"


def _failed(connected: bool, errors: list[str]) -> dict[str, Any]:
    return {"success": False, "duckdb_connected": connected, "errors": errors}


def _run_start(
    project_root: Path,
    setup_result: dict[str, Any],
    console: Console | None = None,
    import_result: dict[str, Any] | None = None,
) -> tuple[Result, MagicMock]:
    """Run the real `dango start` command with every external service replaced by fakes.

    Returns the click result and the mock standing in for the post-setup DuckDB lock step, which
    runs only when the command treats Metabase as configured.
    """
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    lock_step = MagicMock()
    patches = [
        patch.dict(os.environ, {}, clear=False),
        patch("dango.cli.utils.check_v01x_project"),
        patch("dango.cli.utils.require_project_context", return_value=project_root),
        patch("dango.config.ConfigLoader", return_value=config_loader),
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.ensure_duckdb_driver"),
        patch(f"{_STARTUP}.start_docker_services"),
        patch(
            f"{_CREDENTIAL_MIGRATION}.complete_metabase_credential_migration",
            return_value={"status": "not_required"},
        ),
        patch(f"{_STARTUP}.setup_metabase_if_needed", return_value=setup_result),
        patch(f"{_STARTUP}.apply_metabase_duckdb_lock_if_needed", lock_step),
        patch(f"{_STARTUP}.import_dashboards", return_value=import_result),
        patch("dango.cli.helpers.process_manager.start_fastapi_server", return_value=None),
        patch("dango.platform.watcher_lifecycle.kill_orphan_watchers", return_value=0),
        patch("dango.config.credentials.ensure_sensitive_artifact_gitignores", return_value=False),
        patch("socket.socket"),
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        patch("requests.get", return_value=_response()),
        patch("webbrowser.open"),
    ]
    if console is not None:
        patches.append(patch("dango.cli.commands.platform.console", console))
    for p in patches:
        p.start()
    try:
        with patch("socket.socket") as socket_cls:
            socket_cls.return_value.connect_ex.return_value = 1
            result = CliRunner().invoke(start, ["--yes"], obj={})
    finally:
        for p in reversed(patches):
            p.stop()
    return result, lock_step


def _plain(output: str) -> str:
    return re.sub(r"\s+", " ", _ANSI_RE.sub("", output))


def _flat(output: str) -> str:
    """Output without ANSI codes or any whitespace (Rich folds long words across lines)."""
    return re.sub(r"\s+", "", _ANSI_RE.sub("", output))


def _setup_block(output: str) -> str:
    """The text printed between the setup heading and the next section."""
    text = _ANSI_RE.sub("", output)
    start_at = text.index("Checking Metabase setup")
    return text[start_at:]


@pytest.mark.unit
def test_not_connected_does_not_claim_duckdb_connected(tmp_path: Path) -> None:
    result, lock_step = _run_start(tmp_path, _failed(False, [_TIMEOUT_ERROR]))

    text = _plain(result.output)
    assert result.exit_code == 0, result.output
    assert "Metabase setup failed" in text
    assert _CLAIM not in text
    assert "DuckDB is connected" not in text
    lock_step.assert_called_once()  # the platform still starts: metabase_configured stays True


@pytest.mark.unit
def test_not_connected_says_nothing_is_lost_and_rerun_is_safe_with_manual_url(
    tmp_path: Path,
) -> None:
    result, _ = _run_start(tmp_path, _failed(False, [_TIMEOUT_ERROR]))

    text = _plain(result.output).lower()
    assert "metabase is not connected to the warehouse yet" in text
    assert "metabase is running" not in text
    assert "nothing is lost" in text
    assert _RERUN in text
    assert _MANUAL_URL in text


@pytest.mark.unit
def test_connected_keeps_partial_setup_meaning_and_has_no_rerun_sentence(tmp_path: Path) -> None:
    result, lock_step = _run_start(tmp_path, _failed(True, ["Could not save Metabase metadata: x"]))

    text = _plain(result.output)
    assert result.exit_code == 0, result.output
    assert "Metabase setup failed" in text
    assert "partially configured" in text
    assert "DuckDB is connected" in text
    assert _RERUN not in text.lower()
    assert _MANUAL_URL in text
    lock_step.assert_called_once()


@pytest.mark.unit
@pytest.mark.parametrize("connected", [True, False])
def test_rerun_sentence_present_exactly_when_not_connected(tmp_path: Path, connected: bool) -> None:
    result, _ = _run_start(tmp_path, _failed(connected, ["boom"]))

    assert (_RERUN in _plain(result.output).lower()) is (not connected)


@pytest.mark.unit
@pytest.mark.parametrize("connected", [True, False])
def test_missing_duckdb_connected_key_is_treated_as_not_connected(
    tmp_path: Path, connected: bool
) -> None:
    setup_result: dict[str, Any] = {"success": False, "errors": ["boom"]}
    if connected:
        setup_result["duckdb_connected"] = True
    result, _ = _run_start(tmp_path, setup_result)

    assert (_CLAIM in _plain(result.output) or "DuckDB is connected" in _plain(result.output)) is (
        connected
    )


@pytest.mark.unit
def test_read_timeout_is_explained_once_and_detail_line_is_kept(tmp_path: Path) -> None:
    result, _ = _run_start(tmp_path, _failed(False, [_TIMEOUT_ERROR]))

    text = _plain(result.output)
    assert text.count("The request to Metabase timed out") == 1
    assert "Read timed out. (read timeout=10)" in text


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        'Failed to connect DuckDB: {"message":"Timed out after 10.0 s"}',
        "Metabase not ready after 200 seconds",
    ],
)
def test_metabase_side_timeout_text_does_not_claim_the_request_timed_out(
    tmp_path: Path, error: str
) -> None:
    result, _ = _run_start(tmp_path, _failed(False, [error]))

    text = _plain(result.output)
    assert "The request to Metabase timed out" not in text
    assert error.split(": ", 1)[-1][:20] in text


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        "Setup error: HTTPConnectionPool(host='localhost', port=3000): Read timed out. "
        "(read timeout=10)",
        "Setup error: HTTPConnectionPool(host='localhost', port=3000): Max retries exceeded "
        "(Caused by ConnectTimeoutError(<urllib3.connection.HTTPConnection object>, "
        "'Connection to localhost timed out. (connect timeout=10)'))",
    ],
)
def test_client_timeout_text_is_explained_once(tmp_path: Path, error: str) -> None:
    result, _ = _run_start(tmp_path, _failed(False, [error]))

    assert _plain(result.output).count("The request to Metabase timed out") == 1


@pytest.mark.unit
def test_metabase_not_ready_is_not_described_as_running(tmp_path: Path) -> None:
    result, _ = _run_start(tmp_path, _failed(False, ["Metabase not ready after 200 seconds"]))

    text = _plain(result.output).lower()
    assert result.exit_code == 0, result.output
    assert "metabase not ready after 200 seconds" in text
    assert "metabase is running" not in text
    assert "is not connected to the warehouse yet" in text
    assert "retries the connection" not in text


@pytest.mark.unit
def test_admin_credential_failure_does_not_promise_a_connection_retry(tmp_path: Path) -> None:
    error = (
        "Metabase already has an administrator account that Dango cannot sign in to. "
        'Nothing was deleted. Run "dango start" again; if this persists run '
        '"dango metabase repair-admin".'
    )
    result, _ = _run_start(tmp_path, _failed(False, [error]))

    text = _plain(result.output).lower()
    assert result.exit_code == 0, result.output
    assert "dango metabase repair-admin" in text
    assert "retries the connection" not in text
    assert "retries the setup" in text
    assert "names a command to run, run that first" in text


@pytest.mark.unit
def test_items_beyond_the_cap_are_summarised(tmp_path: Path) -> None:
    errors = [f"error-item-{i:02d}" for i in range(12)]
    result, _ = _run_start(tmp_path, _failed(False, errors))

    flat = _flat(result.output)
    assert result.exit_code == 0, result.output
    for i in range(8):
        assert f"error-item-{i:02d}" in flat
    for i in range(8, 12):
        assert f"error-item-{i:02d}" not in flat
    assert "...and4more" in flat


@pytest.mark.unit
def test_duckdb_lock_passage_is_replaced_by_the_plain_explanation(tmp_path: Path) -> None:
    lock = (
        'IO Error: Could not set lock on file "/data/warehouse.duckdb": Conflicting lock is held '
        "in /usr/bin/python3 (PID 4242) by user ed. See also https://example.invalid/concurrency"
    )
    result, _ = _run_start(tmp_path, _failed(False, [f"Failed to connect DuckDB: {lock}"]))

    flat = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert "FailedtoconnectDuckDB:" in flat
    assert "Conflictinglock" not in flat
    assert "Couldnotsetlock" not in flat
    assert "AnotherDangoprocessisusingthewarehouse(PID4242)" in flat


@pytest.mark.unit
def test_timeout_explanation_absent_for_other_errors(tmp_path: Path) -> None:
    result, _ = _run_start(tmp_path, _failed(False, ["Setup error: connection refused"]))

    text = _plain(result.output)
    assert "timed out" not in text
    assert "connection refused" in text


@pytest.mark.unit
def test_no_errors_still_prints_honest_message(tmp_path: Path) -> None:
    result, _ = _run_start(tmp_path, _failed(False, []))

    text = _plain(result.output)
    assert result.exit_code == 0, result.output
    assert "Metabase setup failed" in text
    assert _CLAIM not in text
    assert _RERUN in text.lower()


@pytest.mark.unit
def test_many_errors_are_each_printed_or_summarised(tmp_path: Path) -> None:
    errors = [f"step-{i}-failed" for i in range(3)]
    result, _ = _run_start(tmp_path, _failed(True, errors))

    text = _plain(result.output)
    for item in errors:
        assert item in text


@pytest.mark.unit
def test_very_long_error_is_cut(tmp_path: Path) -> None:
    long_error = "Setup error: " + "x" * 20000
    result, _ = _run_start(tmp_path, _failed(False, [long_error]))

    flat = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert "x" * 100 in flat
    assert "x" * 20000 not in flat
    assert len(flat) < 6000


@pytest.mark.unit
def test_rich_markup_in_error_is_printed_literally(tmp_path: Path) -> None:
    errors = ["Setup error: bad [red]thing[/red] here", "Setup error: unbalanced [ bracket [/oops]"]
    result, _ = _run_start(tmp_path, _failed(False, errors))

    text = _ANSI_RE.sub("", result.output)
    assert result.exit_code == 0, result.output
    assert "bad [red]thing[/red] here" in text
    assert "unbalanced [ bracket [/oops]" in text


@pytest.mark.unit
def test_secrets_in_error_text_are_redacted(tmp_path: Path) -> None:
    errors = [
        "Setup error: could not reach http://admin:hunter2secret@metabase.internal:3000/api",
        "Setup error: failed with api_key=sk_live_abcdefghijklmnop1234",
    ]
    result, _ = _run_start(tmp_path, _failed(False, errors))

    flat = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert "Metabasesetupfailed" in flat
    assert "hunter2secret" not in flat
    assert "sk_live_abcdefghijklmnop1234" not in flat
    assert "metabase.internal" in flat


@pytest.mark.unit
def test_known_project_secret_value_is_redacted(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MY_SECRET_VALUE=zebra-quartz-93417\n", encoding="utf-8")
    result, _ = _run_start(tmp_path, _failed(False, ["Setup error: zebra-quartz-93417 rejected"]))

    flat = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert "Metabasesetupfailed" in flat
    assert "zebra-quartz-93417" not in flat
    assert "rejected" in flat


@pytest.mark.unit
@pytest.mark.parametrize("connected", [True, False])
def test_cp1252_stdout_with_replace_does_not_raise(tmp_path: Path, connected: bool) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="replace", write_through=True)
    cp_console = Console(file=stream, force_terminal=False, width=200)

    result, _ = _run_start(
        tmp_path, _failed(connected, ["Setup error: café ✓ 日本 failed"]), cp_console
    )

    stream.flush()
    assert result.exit_code == 0, result.output
    assert "Metabase setup failed" in raw.getvalue().decode("cp1252", errors="replace")


@pytest.mark.unit
def test_success_branch_unchanged(tmp_path: Path) -> None:
    result, lock_step = _run_start(
        tmp_path,
        {"success": True, "collections_created": ["Raw", "Marts"], "errors": []},
    )

    block = _setup_block(result.output)
    assert result.exit_code == 0, result.output
    assert "✓ Metabase configured automatically" in block
    assert "Collections: Raw, Marts" in block
    assert "Metabase setup failed" not in block
    assert "partially configured" not in block
    lock_step.assert_called_once()


@pytest.mark.unit
def test_already_configured_branch_unchanged(tmp_path: Path) -> None:
    result, lock_step = _run_start(tmp_path, {"already_configured": True, "success": True})

    block = _setup_block(result.output)
    assert result.exit_code == 0, result.output
    assert "✓ Metabase already configured" in block
    assert "Metabase setup failed" not in block
    lock_step.assert_called_once()


@pytest.mark.unit
def test_skipped_branch_unchanged(tmp_path: Path) -> None:
    result, lock_step = _run_start(
        tmp_path,
        {"success": True, "skipped": True, "skip_reason": "No usable admin email."},
    )

    block = _plain(_setup_block(result.output))
    assert result.exit_code == 0, result.output
    assert "⚠ Metabase was not set up" in block
    assert "No usable admin email." in block
    assert "dango auth add-user you@yourcompany.com --role admin --password" in block
    assert "Metabase setup failed" not in block
    lock_step.assert_not_called()


@pytest.mark.unit
def test_runtime_error_branch_unchanged_still_aborts(tmp_path: Path) -> None:
    with patch(f"{_STARTUP}.setup_metabase_if_needed", side_effect=RuntimeError("boom")):
        config_loader = MagicMock()
        config_loader.load_config.return_value = _config()
        manager = MagicMock()
        with (
            patch("dango.cli.utils.check_v01x_project"),
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", return_value=config_loader),
            patch(f"{_STARTUP}.check_duckdb_version_alignment"),
            patch(f"{_STARTUP}.rotate_logs"),
            patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
            patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False),
            patch(f"{_STARTUP}.ensure_dbt_schemas"),
            patch(f"{_STARTUP}.ensure_duckdb_driver"),
            patch(f"{_STARTUP}.start_docker_services"),
            patch(
                f"{_CREDENTIAL_MIGRATION}.complete_metabase_credential_migration",
                return_value={"status": "not_required"},
            ),
            patch("dango.platform.DockerManager", return_value=manager),
            patch("dango.config.credentials.ensure_sensitive_artifact_gitignores"),
            patch("dango.platform.watcher_lifecycle.kill_orphan_watchers", return_value=0),
            patch("socket.socket") as socket_cls,
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        ):
            socket_cls.return_value.connect_ex.return_value = 1
            result = CliRunner().invoke(start, ["--yes"], obj={})

    assert result.exit_code != 0
    assert "Critical error: Cannot connect Metabase to DuckDB" in result.output
    manager.stop_services.assert_called_once()
