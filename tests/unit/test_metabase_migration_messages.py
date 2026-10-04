"""tests/unit/test_metabase_migration_messages.py

Verify each credential-migration failure reason is classified once and that start/serve
print the matching message (only transient reasons promise a retry).
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

import dango.platform.common.metabase_credential_migration as migration
from dango.cli.commands.platform import start
from dango.cli.commands.serve import serve

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_MIGRATION = "dango.platform.common.metabase_credential_migration"
_PERMANENT = {"status": "failed_non_destructive", "reason": "current_credential_not_accepted"}


@pytest.mark.unit
class TestClassification:
    def test_every_failure_reason_is_classified_exactly_once(self) -> None:
        source = inspect.getsource(migration)
        reasons = set(re.findall(r'_failed\(\w+, "([a-z_]+)"', source))
        assert len(reasons) == 15
        for reason in reasons:
            in_retryable = reason in migration._RETRYABLE_REASONS
            in_permanent = reason in migration._PERMANENT_MESSAGES
            assert in_retryable != in_permanent, f"{reason} must be in exactly one set"
        assert migration._RETRYABLE_REASONS | set(migration._PERMANENT_MESSAGES) == reasons

    def test_permanent_reasons_never_say_retry(self) -> None:
        for reason, text in migration._PERMANENT_MESSAGES.items():
            message, retryable = migration.describe_migration_failure(
                {"status": "failed_non_destructive", "reason": reason}
            )
            assert message == text
            assert retryable is False
            assert "retry" not in message.lower()

    def test_retryable_reasons_and_unknown_use_the_retry_message(self) -> None:
        for reason in [*migration._RETRYABLE_REASONS, "some_future_reason"]:
            message, retryable = migration.describe_migration_failure({"reason": reason})
            assert retryable is True
            assert "will retry on the next start" in message

    def test_missing_reason_defaults_to_retry_message(self) -> None:
        message, retryable = migration.describe_migration_failure(
            {"status": "failed_non_destructive"}
        )
        assert retryable is True
        assert message == migration._RETRYABLE_MESSAGE


def _config() -> MagicMock:
    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    config.platform.metabase_port = 3000
    config.platform.dbt_docs_port = 8081
    config.platform.auto_sync = False
    return config


def _common_patches(project_root: Path, migration_result: dict[str, object]) -> list[Any]:
    config_loader = MagicMock()
    config_loader.load_config.return_value = _config()
    return [
        patch.dict(os.environ, {}, clear=False),
        patch("dango.cli.utils.require_project_context", return_value=project_root),
        patch("dango.config.ConfigLoader", return_value=config_loader),
        patch("dango.config.credentials.ensure_sensitive_artifact_gitignores", return_value=False),
        patch(f"{_STARTUP}.check_duckdb_version_alignment"),
        patch(f"{_STARTUP}.rotate_logs"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", return_value=False),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.ensure_duckdb_driver"),
        patch(f"{_STARTUP}.start_docker_services"),
        patch(
            f"{_MIGRATION}.complete_metabase_credential_migration", return_value=migration_result
        ),
        patch(f"{_STARTUP}.setup_metabase_if_needed", return_value={"success": True}),
        patch(f"{_STARTUP}.import_dashboards", return_value=None),
    ]


def _run_local(project_root: Path, migration_result: dict[str, object]) -> Result:
    patches = _common_patches(project_root, migration_result) + [
        patch("dango.cli.utils.check_v01x_project"),
        patch("dango.cli.helpers.process_manager.start_fastapi_server", return_value=None),
        patch("dango.platform.watcher_lifecycle.kill_orphan_watchers", return_value=0),
        patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
        patch("requests.get", return_value=MagicMock(status_code=200)),
        patch("webbrowser.open"),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        socket_cls = stack.enter_context(patch("socket.socket"))
        socket_cls.return_value.connect_ex.return_value = 1
        return CliRunner().invoke(start, ["--yes"], obj={})


def _run_cloud(project_root: Path, migration_result: dict[str, object]) -> Result:
    patches = _common_patches(project_root, migration_result) + [
        patch("dango.cli.commands.serve._check_port"),
        patch("dango.cli.commands.serve._stop_docker_quiet"),
        patch("uvicorn.run"),
    ]
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        return CliRunner().invoke(serve, [], obj={})


@pytest.mark.unit
@pytest.mark.parametrize("run_command", [_run_local, _run_cloud])
def test_local_start_and_cloud_serve_print_the_permanent_message(
    tmp_path: Path, run_command: Callable[[Path, dict[str, object]], Result]
) -> None:
    result = run_command(tmp_path, _PERMANENT)
    output = " ".join(_ANSI_RE.sub("", result.output).split())

    assert result.exit_code == 0, result.output
    assert "could not sign in" in output
    assert "will retry" not in output
