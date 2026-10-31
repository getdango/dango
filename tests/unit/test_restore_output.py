"""tests/unit/test_restore_output.py

What the user sees from restore, rollback and push: server-derived text is printed literally (a
``[/]`` in a path is not Rich markup), a Spaces restore refusal is one clean line, and the notes
the server-side restore used to only log (auth.yml kept, WAL kept aside, Metabase metadata
omitted) come back as warnings.
"""

from __future__ import annotations

import io
import types
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.remote import remote
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import backup
from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_shell import run_spaces_restore

pytestmark = pytest.mark.unit

BRACKETS = "notebooks/a[/] and [bold]x[/bold]"
_LOADER = "dango.config.loader.ConfigLoader"
_CTX = "dango.cli.utils.require_project_context"
_SSH = "dango.platform.cloud.ssh.SSHManager"


def _invoke(tmp_path: Path, args: list[str], ssh: MagicMock) -> Result:
    cfg = MagicMock(droplet_id=42, droplet_ip="1.2.3.4", ssh_key_path=".dango/cloud_key")
    loader = MagicMock()
    loader.load_cloud_config.return_value = cfg
    with (
        patch(_CTX, return_value=tmp_path),
        patch(_LOADER, return_value=loader),
        patch(_SSH, return_value=ssh),
    ):
        return CliRunner().invoke(remote, args, obj={"project_root": tmp_path})


def _ssh(stdout: str = "", stderr: str = "", code: int = 0) -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=stdout, stderr=stderr, exit_code=code)
    ssh.connect.return_value = ssh
    return ssh


def _restore_result(warnings: list[str]) -> backup.RestoreResult:
    return backup.RestoreResult(
        restored_from="/x",
        services_restarted=True,
        health_check_passed=True,
        duration_seconds=1.0,
        warnings=warnings,
    )


# --- L3: literal server text ---------------------------------------------------------------------


def test_rollback_prints_warnings_and_errors_literally(tmp_path: Path) -> None:
    with patch("dango.platform.cloud.backup.rollback", return_value=_restore_result([BRACKETS])):
        out = _invoke(tmp_path, ["rollback", "-y"], _ssh())
    assert out.exit_code == 0, out.output
    assert BRACKETS in out.output
    with patch("dango.platform.cloud.backup.rollback", side_effect=RuntimeError(BRACKETS)):
        out = _invoke(tmp_path, ["rollback", "-y"], _ssh())
    assert out.exit_code == 1 and BRACKETS in out.output
    assert "MarkupError" not in repr(out.exception)


def _push_result(warnings: list[str]) -> types.SimpleNamespace:
    sync = types.SimpleNamespace(
        synced_files=[],
        added_models=[],
        changed_models=[],
        removed_models=[],
        packages_changed=False,
        port_changes=None,
    )
    return types.SimpleNamespace(
        sync_result=sync,
        git_info=None,
        dbt_deps_run=False,
        dbt_compile_success=False,
        models_rebuilt=[],
        duration_seconds=1.0,
        warnings=warnings,
        port_notice=None,
    )


@pytest.fixture
def push_env(tmp_path: Path) -> Iterator[MagicMock]:
    cfg = MagicMock(droplet_ip="1.2.3.4", ssh_key_path="k", deploy_branch=None)
    ssh = MagicMock()
    with (
        patch("dango.cli.commands.remote._require_cloud_deployment", return_value=(cfg, tmp_path)),
        patch("dango.cli.commands.remote._connect_ssh", return_value=ssh),
        patch("dango.utils.git_info.collect_git_info", return_value=MagicMock(is_git_repo=False)),
        patch("dango.platform.cloud.server_ports.format_port_notice", return_value=None),
    ):
        yield ssh


def test_push_prints_warnings_and_errors_literally(tmp_path: Path, push_env: MagicMock) -> None:
    with patch("dango.platform.cloud.deployer.push_deploy", return_value=_push_result([BRACKETS])):
        out = CliRunner().invoke(remote, ["push", "-y"], obj={"project_root": tmp_path})
    assert out.exit_code == 0, out.output
    assert "Push complete" in out.output and BRACKETS in out.output
    with patch("dango.platform.cloud.deployer.push_deploy", side_effect=RuntimeError(BRACKETS)):
        out = CliRunner().invoke(remote, ["push", "-y"], obj={"project_root": tmp_path})
    assert out.exit_code == 1 and BRACKETS in out.output


def test_from_local_restore_prints_errors_literally(tmp_path: Path) -> None:
    archive = tmp_path / "backup-20260224-143000.tar.gz"
    archive.write_bytes(b"x")
    with patch(
        "dango.platform.cloud.backup.restore_from_archive", side_effect=RuntimeError(BRACKETS)
    ):
        out = _invoke(tmp_path, ["backup", "restore", str(archive), "--from-local", "-y"], _ssh())
    assert out.exit_code == 1 and BRACKETS in out.output


# --- L4: a clean Spaces refusal ------------------------------------------------------------------


def test_a_spaces_refusal_is_one_clean_line_without_a_traceback(tmp_path: Path) -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    msg = "Archive is incomplete: manifest.json lists 1 file(s) that are not in the archive."
    ssh = _ssh(
        stdout=f"{checks.RESTORE_ERROR_PREFIX}{msg}\n",
        stderr="Traceback (most recent call last):\n  File x\nCloudProvisioningError: " + msg,
        code=1,
    )
    out = _invoke(tmp_path, ["backup", "restore", "backup-20260224-143000.tar.gz", "-y"], ssh)
    assert out.exit_code == 1
    assert msg in out.output
    assert "Traceback" not in out.output and "corrupt" not in out.output


def test_the_server_script_turns_an_exception_into_one_marked_line() -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    script = checks.spaces_restore_script("backups/backup-1.tar.gz")
    boom = CloudProvisioningError("first line\nsecond [x] line")
    with (
        patch("dango.platform.cloud.scheduled_backup.restore_from_spaces", side_effect=boom),
        patch("dango.platform.cloud.scheduled_backup._load_spaces_config", return_value={}),
        redirect_stdout(io.StringIO()) as buf,
        pytest.raises(SystemExit) as exit_info,
    ):
        exec(script, {"__name__": "__main__"})
    assert exit_info.value.code == 1
    assert buf.getvalue() == f"{checks.RESTORE_ERROR_PREFIX}first line second [x] line\n"


def test_the_server_script_prints_each_warning_as_one_marked_line() -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    script = checks.spaces_restore_script("backups/backup-1.tar.gz")
    with (
        patch(
            "dango.platform.cloud.scheduled_backup.restore_from_spaces",
            return_value=["one\ntwo", "three"],
        ),
        patch("dango.platform.cloud.scheduled_backup._load_spaces_config", return_value={}),
        redirect_stdout(io.StringIO()) as buf,
    ):
        exec(script, {"__name__": "__main__"})
    p = checks.RESTORE_WARNING_PREFIX
    assert buf.getvalue() == f"{p}one two\n{p}three\n"


def test_the_client_sends_that_script(tmp_path: Path) -> None:
    ssh = _ssh()
    out = _invoke(tmp_path, ["backup", "restore", "backup-20260224-143000.tar.gz", "-y"], ssh)
    assert out.exit_code == 0, out.output
    sent = ssh.exec_command.call_args_list[-1].args[0]
    assert "except Exception" in sent and "restore_from_spaces" in sent


# --- L4: notes the server-side restore only logged come back as warnings --------------------------


def test_spaces_restore_returns_the_wal_auth_and_metabase_notes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / "data" / "warehouse.duckdb.wal").write_text("live wal")
    top = "backup-20260101-000000"
    content = tmp_path / top
    (content / "data").mkdir(parents=True)
    (content / "data" / "warehouse.duckdb").write_text("archive")
    (content / ".dango").mkdir()
    (content / ".dango" / "auth.yml").write_text("enabled: false\n")
    (content / ".dango" / "metabase.yml").write_text("a: [unclosed\n")
    (content / "manifest.json").write_text("{}")
    returned: list[list[str]] = []
    run_spaces_restore(tmp_path, project, top, returned=returned)
    text = " | ".join(returned[0])
    assert "warehouse.duckdb.wal" in text and "pre-restore" in text
    assert "auth.yml" in text
    assert "Metabase metadata" in text
