"""tests/unit/test_metabase_credential_lifecycle_commands.py

Verify credential-migration completion ordering in local and cloud commands.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


def _run_local(project_root: Path, migration_result: dict[str, object]) -> tuple[Result, list[str]]:
    """Run ``dango start`` with all external services replaced by ordered fakes."""
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    events: list[str] = []

    with (
        patch("dango.cli.utils.check_v01x_project"),
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
            side_effect=lambda _root: events.append("completion") or migration_result,
        ),
        patch(
            f"{_STARTUP}.setup_metabase_if_needed",
            side_effect=lambda *_args: events.append("setup") or {"success": True},
        ),
        patch(f"{_STARTUP}.import_dashboards", return_value=None),
        patch("dango.cli.helpers.process_manager.start_fastapi_server", return_value=None),
        patch("dango.platform.watcher_lifecycle.kill_orphan_watchers", return_value=0),
        patch("socket.socket") as socket_cls,
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        patch("requests.get", return_value=_response()),
        patch("webbrowser.open"),
    ):
        socket_cls.return_value.connect_ex.return_value = 1
        result = CliRunner().invoke(start, ["--yes"], obj={})
    return result, events


def _run_cloud(project_root: Path, migration_result: dict[str, object]) -> tuple[Result, list[str]]:
    """Run ``dango serve`` with all external services replaced by ordered fakes."""
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    events: list[str] = []

    with (
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
            side_effect=lambda _root: events.append("completion") or migration_result,
        ),
        patch(
            f"{_STARTUP}.setup_metabase_if_needed",
            side_effect=lambda *_args: events.append("setup") or {"success": True},
        ),
        patch(f"{_STARTUP}.import_dashboards", return_value=None),
        patch("dango.cli.commands.serve._check_port"),
        patch("dango.cli.commands.serve._stop_docker_quiet"),
        patch("uvicorn.run"),
    ):
        result = CliRunner().invoke(serve, [], obj={})
    return result, events


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_completion_runs_after_docker_and_before_setup(
    tmp_path: Path,
    run_command: CommandRunner,
) -> None:
    """Both lifecycle commands complete migration between Docker and setup."""
    result, events = run_command(tmp_path, {"status": "secure_rotated"})

    assert result.exit_code == 0, result.output
    assert events == ["docker", "completion", "setup"]


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_completion_failure_warns_and_preserves_setup(
    tmp_path: Path,
    run_command: CommandRunner,
) -> None:
    """A retryable completion failure never suppresses normal Metabase setup."""
    result, events = run_command(tmp_path, {"status": "failed_non_destructive"})
    plain_output = _ANSI_RE.sub("", result.output)

    assert result.exit_code == 0, result.output
    assert events == ["docker", "completion", "setup"]
    assert "Metabase credential migration is incomplete" in plain_output
    assert re.search(r"existing\s+configuration\s+is\s+unchanged", plain_output)


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_not_required_completion_is_silent_and_non_disruptive(
    tmp_path: Path,
    run_command: CommandRunner,
) -> None:
    """Password-free projects do not receive migration noise or behavior changes."""
    result, events = run_command(tmp_path, {"status": "not_required"})

    assert result.exit_code == 0, result.output
    assert events == ["docker", "completion", "setup"]
    assert "Metabase credential migration is incomplete" not in result.output
