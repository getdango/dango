"""tests/unit/test_start_serve_metabase_ready_order.py

Verify `dango start` and `dango serve` wait for Metabase before the credential
migration and dashboard import, and never abort when the wait fails.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.platform import start
from dango.cli.commands.serve import serve

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_CREDENTIAL_MIGRATION = "dango.platform.common.metabase_credential_migration"
CommandRunner = Callable[[Path, dict[str, object]], tuple[Result, list[str]]]


def _config() -> MagicMock:
    """Return the minimal project configuration both command paths require."""
    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    config.platform.metabase_port = 3000
    config.platform.dbt_docs_port = 8081
    config.platform.auto_sync = False
    return config


def _response() -> MagicMock:
    """Return a healthy HTTP response for local start's final readiness probes."""
    response = MagicMock()
    response.status_code = 200
    return response


def _run_local(
    project_root: Path,
    wait_result: bool | None | Exception = True,
    work_pending: bool = False,
) -> tuple[Result, list[str]]:
    """Run ``dango start`` with all external services replaced by ordered fakes."""
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    events: list[str] = []

    def _harden_gitignore(_root: Path) -> bool:
        events.append("gitignore")
        return False

    def _wait(_root: Path) -> bool | None:
        events.append("wait")
        if isinstance(wait_result, Exception):
            raise wait_result
        return wait_result

    ignore_patcher = patch(
        "dango.config.credentials.ensure_sensitive_artifact_gitignores",
        side_effect=_harden_gitignore,
    )
    ignore_patcher.start()

    # Entered via ExitStack: the combined ``with`` below would exceed Python's
    # static block-nesting limit if these two patches were added to it.
    stack = ExitStack()
    stack.enter_context(patch(f"{_STARTUP}.wait_for_metabase_if_needed", side_effect=_wait))
    stack.enter_context(
        patch(f"{_STARTUP}.metabase_startup_work_pending", return_value=work_pending)
    )

    for quiet in ("rotate_logs", "ensure_dbt_schemas", "ensure_duckdb_driver"):
        stack.enter_context(patch(f"{_STARTUP}.{quiet}"))
    stack.enter_context(patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False))

    with (
        stack,
        patch.dict(os.environ, {}, clear=False),
        patch("dango.cli.utils.check_v01x_project"),
        patch("dango.cli.utils.require_project_context", return_value=project_root),
        patch("dango.config.ConfigLoader", return_value=config_loader),
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(
            f"{_STARTUP}.start_docker_services",
            side_effect=lambda _root: events.append("docker"),
        ),
        patch(
            f"{_CREDENTIAL_MIGRATION}.complete_metabase_credential_migration",
            side_effect=lambda _root: events.append("completion") or {"status": "secure_rotated"},
        ),
        patch(
            f"{_STARTUP}.setup_metabase_if_needed",
            side_effect=lambda *_args: events.append("setup") or {"success": True},
        ),
        patch(
            f"{_STARTUP}.import_dashboards",
            side_effect=lambda _root: events.append("import"),
        ),
        patch("dango.cli.helpers.process_manager.start_fastapi_server", return_value=None),
        patch("dango.platform.watcher_lifecycle.kill_orphan_watchers", return_value=0),
        patch("socket.socket") as socket_cls,
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        patch("requests.get", return_value=_response()),
        patch("webbrowser.open"),
    ):
        socket_cls.return_value.connect_ex.return_value = 1
        result = CliRunner().invoke(start, ["--yes"], obj={})
    ignore_patcher.stop()
    return result, events


def _run_cloud(
    project_root: Path,
    wait_result: bool | None | Exception = True,
    work_pending: bool = False,
) -> tuple[Result, list[str]]:
    """Run ``dango serve`` with all external services replaced by ordered fakes."""
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    events: list[str] = []

    def _harden_gitignore(_root: Path) -> bool:
        events.append("gitignore")
        return False

    def _wait(_root: Path) -> bool | None:
        events.append("wait")
        if isinstance(wait_result, Exception):
            raise wait_result
        return wait_result

    ignore_patcher = patch(
        "dango.config.credentials.ensure_sensitive_artifact_gitignores",
        side_effect=_harden_gitignore,
    )
    ignore_patcher.start()

    with (
        patch(f"{_STARTUP}.wait_for_metabase_if_needed", side_effect=_wait),
        patch.dict(os.environ, {}, clear=False),
        patch("dango.cli.utils.require_project_context", return_value=project_root),
        patch("dango.config.ConfigLoader", return_value=config_loader),
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.ensure_duckdb_driver"),
        patch(
            f"{_STARTUP}.start_docker_services",
            side_effect=lambda _root: events.append("docker"),
        ),
        patch(
            f"{_CREDENTIAL_MIGRATION}.complete_metabase_credential_migration",
            side_effect=lambda _root: events.append("completion") or {"status": "secure_rotated"},
        ),
        patch(
            f"{_STARTUP}.setup_metabase_if_needed",
            side_effect=lambda *_args: events.append("setup") or {"success": True},
        ),
        patch(
            f"{_STARTUP}.import_dashboards",
            side_effect=lambda _root: events.append("import"),
        ),
        patch("dango.cli.commands.serve._check_port"),
        patch("dango.cli.commands.serve._stop_docker_quiet"),
        patch("uvicorn.run"),
    ):
        result = CliRunner().invoke(serve, [], obj={})
    ignore_patcher.stop()
    return result, events


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_wait_runs_after_gitignore_before_migration_and_setup(
    tmp_path: Path,
    run_command: CommandRunner,
) -> None:
    """The wait sits between gitignore hardening and the migration/setup/import."""
    result, events = run_command(tmp_path, True)

    assert result.exit_code == 0, result.output
    assert events[:5] == ["docker", "gitignore", "wait", "completion", "setup"]
    assert events[-1] == "import"
    assert events.index("setup") < events.index("import")


@pytest.mark.unit
@pytest.mark.parametrize("pending", [True, False])
def test_local_start_prints_waiting_line_only_when_pending(tmp_path: Path, pending: bool) -> None:
    """The progress line appears only when startup actually has Metabase work."""
    result, _events = _run_local(tmp_path, True, pending)

    assert result.exit_code == 0, result.output
    assert ("Waiting for Metabase to be ready" in _ANSI_RE.sub("", result.output)) is pending


@pytest.mark.unit
def test_local_start_prints_retry_hint_on_timeout_and_still_starts(tmp_path: Path) -> None:
    """A wait timeout warns and leaves the migration and setup steps running."""
    result, events = _run_local(tmp_path, False, True)
    plain_output = _ANSI_RE.sub("", result.output)

    assert result.exit_code == 0, result.output
    assert re.search(r"will\s+retry\s+on\s+the\s+next\s+start", plain_output)
    assert "completion" in events
    assert "setup" in events


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_start_and_serve_survive_wait_raising(
    tmp_path: Path,
    run_command: CommandRunner,
) -> None:
    """A wait failure never aborts either lifecycle command."""
    result, events = run_command(tmp_path, RuntimeError("boom"))

    assert result.exit_code == 0, result.output
    assert "completion" in events
    assert "setup" in events


@pytest.mark.unit
def test_cloud_serve_timeout_warns_on_stderr_and_continues(tmp_path: Path) -> None:
    """Serve reports the timeout on stderr and still runs migration and setup."""
    result, events = _run_cloud(tmp_path, False)
    plain_output = _ANSI_RE.sub("", result.output)

    assert result.exit_code == 0, result.output
    assert re.search(r"will\s+retry\s+on\s+the\s+next\s+restart", plain_output)
    assert "completion" in events
    assert "setup" in events
