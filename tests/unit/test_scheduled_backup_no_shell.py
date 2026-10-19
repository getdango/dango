"""tests/unit/test_scheduled_backup_no_shell.py

Scheduled backup runs local commands as argument lists, never through a shell.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import scheduled_backup

_MODULE = "dango.platform.cloud.scheduled_backup"
_SOURCE = Path(scheduled_backup.__file__)


@pytest.mark.unit
def test_no_shell_true_in_scheduled_backup() -> None:
    offenders = []
    for node in ast.walk(ast.parse(_SOURCE.read_text())):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False
            ):
                offenders.append(node.lineno)
    assert offenders == []


@pytest.mark.unit
def test_run_local_receives_arg_lists() -> None:
    with patch(f"{_MODULE}.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="out", stderr="")
        assert scheduled_backup._run_local(["echo", "a b"], step="t") == "out"
    assert run.call_args.args[0] == ["echo", "a b"]
    assert "shell" not in run.call_args.kwargs


@pytest.mark.unit
def test_run_local_failure_and_timeout_raise() -> None:
    with patch(f"{_MODULE}.subprocess.run") as run:
        run.return_value = MagicMock(returncode=2, stdout="", stderr="boom")
        with pytest.raises(CloudProvisioningError, match="exit 2"):
            scheduled_backup._run_local(["false"], step="t")
        run.side_effect = subprocess.TimeoutExpired("x", 1)
        with pytest.raises(CloudProvisioningError, match="timed out"):
            scheduled_backup._run_local(["sleep"], step="t")


@pytest.mark.unit
def test_service_commands_are_arg_lists() -> None:
    calls: list[Any] = []

    def fake_run(args: Any, **kwargs: Any) -> MagicMock:
        calls.append((args, kwargs))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch(f"{_MODULE}.subprocess.run", side_effect=fake_run),
        patch(f"{_MODULE}.get_compose_project_name", return_value="proj"),
    ):
        scheduled_backup._stop_services()
        scheduled_backup._start_services()
        scheduled_backup.enable_scheduled_backup()
        scheduled_backup.disable_scheduled_backup()
        scheduled_backup.is_scheduled_backup_enabled()
    assert calls
    for args, kwargs in calls:
        assert isinstance(args, list)
        assert not kwargs.get("shell")
    assert ["systemctl", "stop", "dango-web"] in [c[0] for c in calls]
    compose_env = [kw["env"] for a, kw in calls if a[0] == "docker"]
    assert compose_env
    assert all(env["COMPOSE_PROJECT_NAME"] == "proj" for env in compose_env)


@pytest.mark.unit
def test_staging_path_with_quote_is_safe(tmp_path: Path) -> None:
    """A path holding a quote and a space reaches the program as one argument."""
    staging = tmp_path / "it's a dir"
    staging.mkdir()
    (staging / "f").write_text("x")
    scheduled_backup._run_local(["ls", str(staging)], step="t")
    out = scheduled_backup._run_local(["ls", str(staging)], step="t")
    assert out.strip() == "f"
    with patch(f"{_MODULE}.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        scheduled_backup._run_local(["rm", "-rf", str(staging)], step="t")
    assert run.call_args.args[0] == ["rm", "-rf", str(staging)]


@pytest.mark.unit
def test_verify_upload_logs_failure() -> None:
    with (
        patch(f"{_MODULE}._make_spaces_client", side_effect=RuntimeError("down")),
        patch(f"{_MODULE}._logger") as logger,
    ):
        assert scheduled_backup._verify_upload({}, "backups/x.tar.gz", 1) is False
    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[0] == "backup_upload_verification_failed"
    assert logger.warning.call_args.kwargs["key"] == "backups/x.tar.gz"


@pytest.mark.unit
def test_services_restart_when_stop_is_interrupted(tmp_path: Path) -> None:
    """A signal during the service stop still restarts services."""
    with (
        patch(f"{_MODULE}._stop_services", side_effect=KeyboardInterrupt),
        patch(f"{_MODULE}._start_services") as start,
        patch(f"{_MODULE}.BACKUP_DIR", tmp_path),
        pytest.raises(KeyboardInterrupt),
    ):
        scheduled_backup._create_local_archive("daily")
    start.assert_called_once()
