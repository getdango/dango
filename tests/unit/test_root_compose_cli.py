"""tests/unit/test_root_compose_cli.py

``remote repair``, ``remote reset-metabase`` and ``deploy destroy`` (BYOS) reach Docker through
the compose helper when the marker exists; a busy helper stops with a retry hint and no partial state.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.deploy import deploy
from dango.cli.commands.remote_repair import remote_repair, remote_reset_metabase
from dango.platform.cloud import compose_helper as ch
from tests.factories.root_compose_fakes import (
    FakeRootSSH,
    assert_no_project_compose_on_helper_branch,
)

pytestmark = pytest.mark.unit

_ID = "ab12cd34" * 4
_PROJECT = "dango-ab12cd34"


class _CliSSH(FakeRootSSH):
    def exec_command(self, command, timeout=None, **kw):  # type: ignore[no-untyped-def]
        result = super().exec_command(command, timeout=timeout, **kw)
        if "MetabaseCredentialStore" in command:
            from dango.platform.cloud.ssh import CommandResult

            return CommandResult(_ID + "\n", "", 0)
        return result


def _patches(ssh: FakeRootSSH):
    cfg = MagicMock(droplet_ip="203.0.113.1")
    return (
        patch(
            "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
            return_value=(cfg, Path("/project")),
        ),
        patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value=_PROJECT),
        patch("time.sleep"),
    )


def _invoke(command, ssh: FakeRootSSH, **kwargs):  # type: ignore[no-untyped-def]
    p = _patches(ssh)
    with p[0], p[1], p[2], p[3]:
        return CliRunner().invoke(command, **kwargs)


def _floor(verb: str) -> int:
    return ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET[verb] + ch.CALLER_MARGIN


class TestRepairStart:
    def test_helper_start_metabase(self) -> None:
        ssh = _CliSSH(helper=True)
        result = _invoke(remote_repair, ssh)
        assert result.exit_code == 0, result.output
        assert "helper:start metabase:0" in ssh.events
        assert_no_project_compose_on_helper_branch(ssh)
        idx = next(i for i, c in enumerate(ssh.commands) if "start metabase" in c)
        assert ssh.timeouts[idx] >= _floor("start")

    def test_busy_reports_retry_and_still_restarts_dango_web(self) -> None:
        ssh = _CliSSH(helper=True, rc={"start metabase": 75})
        result = _invoke(remote_repair, ssh)
        assert "retry" in result.output.lower()
        assert "Docker operation" in result.output
        assert ssh.events[-1] == "web:start"

    def test_legacy_command_unchanged(self) -> None:
        ssh = _CliSSH(helper=False)
        _invoke(remote_repair, ssh)
        assert (
            f"legacy:cd /srv/dango/project && COMPOSE_PROJECT_NAME={_PROJECT} docker compose "
            "start metabase 2>/dev/null || true"
        ) in ssh.events


class TestResetMetabase:
    def test_helper_down_volumes(self) -> None:
        ssh = _CliSSH(helper=True)
        result = _invoke(remote_reset_metabase, ssh, input="reset\n")
        assert result.exit_code == 0, result.output
        assert "helper:down-volumes metabase:0" in ssh.events
        assert_no_project_compose_on_helper_branch(ssh)
        idx = next(i for i, c in enumerate(ssh.commands) if "down-volumes" in c)
        assert ssh.timeouts[idx] >= _floor("down-volumes")

    def test_busy_aborts_restarts_web_and_removes_nothing(self) -> None:
        ssh = _CliSSH(helper=True, rc={"down-volumes metabase": 75})
        result = _invoke(remote_reset_metabase, ssh, input="reset\n")
        assert result.exit_code != 0
        assert "retry" in result.output.lower()
        assert ssh.events[-1] == "web:start"
        assert not any("metabase.yml" in c or "rm -f /srv/dango/secrets" in c for c in ssh.commands)

    def test_legacy_command_unchanged(self) -> None:
        ssh = _CliSSH(helper=False)
        result = _invoke(remote_reset_metabase, ssh, input="reset\n")
        assert result.exit_code == 0, result.output
        assert (
            f"legacy:cd /srv/dango/project && COMPOSE_PROJECT_NAME={_PROJECT} docker compose "
            "down metabase --volumes 2>/dev/null || true"
        ) in ssh.events


def _destroy(ssh: FakeRootSSH, tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = MagicMock(provider="byos", droplet_ip="1.2.3.4", domain=None, ssh_key_path="k")
    (tmp_path / ".dango").mkdir()
    cloud_yml = tmp_path / ".dango" / "cloud.yml"
    cloud_yml.write_text("x")
    key = tmp_path / "key"
    key.write_text("k")
    with (
        patch("dango.cli.commands.deploy._load_deploy_config", return_value=(cfg, tmp_path)),
        patch("dango.cli.commands.deploy._resolve_key_path", return_value=key),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value=_PROJECT),
    ):
        result = CliRunner().invoke(deploy, ["destroy", "--force"])
    return result, cloud_yml


class TestDestroy:
    def test_helper_destroy(self, tmp_path: Path) -> None:
        ssh = FakeRootSSH(helper=True)
        result, _ = _destroy(ssh, tmp_path)
        assert result.exit_code == 0, result.output
        assert "helper:destroy:0" in ssh.events
        assert_no_project_compose_on_helper_branch(ssh)
        idx = next(i for i, c in enumerate(ssh.commands) if "dango-compose destroy" in c)
        assert ssh.timeouts[idx] >= _floor("destroy")

    def test_busy_aborts_before_removing_anything(self, tmp_path: Path) -> None:
        ssh = FakeRootSSH(helper=True, rc={"destroy": 75})
        result, cloud_yml = _destroy(ssh, tmp_path)
        assert result.exit_code != 0
        assert "retry" in result.output.lower()
        assert not any(c.startswith("rm -rf /srv/dango") for c in ssh.commands)
        assert cloud_yml.exists()

    def test_legacy_command_unchanged(self, tmp_path: Path) -> None:
        ssh = FakeRootSSH(helper=False)
        result, _ = _destroy(ssh, tmp_path)
        assert result.exit_code == 0, result.output
        assert (
            f"legacy:cd /srv/dango/project && COMPOSE_PROJECT_NAME={_PROJECT} docker compose "
            "down --volumes --rmi local 2>/dev/null || true"
        ) in ssh.events
