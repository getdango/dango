"""tests/unit/test_g3f3_small_lows.py

Small Gate 3 fixes: create lookup errors, empty-guard cwd, BYOS setup error, push SSH error, DNS note.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudError, CloudProvisioningError, CloudSSHError
from dango.platform.cloud.ssh import CommandResult


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [ValueError("not json"), TypeError("odd"), AttributeError("odd"), KeyError("id")],
    ids=["ValueError", "TypeError", "AttributeError", "KeyError"],
)
def test_odd_lookup_answer_still_says_the_resource_may_exist(error: Exception) -> None:
    from dango.platform.cloud.create_reconcile import reconcile_create

    def _find() -> list[dict[str, Any]]:
        raise error

    with pytest.raises(CloudError, match=r"may exist; check the DigitalOcean console"):
        reconcile_create("droplet", "dango-x", "HTTP 502", _find)


@pytest.mark.unit
def test_empty_guard_probe_runs_from_the_root_directory(tmp_path: Path) -> None:
    from dango.platform.cloud.empty_guard import refuse_empty_mirrors

    (tmp_path / "dbt" / "models").mkdir(parents=True)
    commands: list[str] = []

    def _exec(command: str, **kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(stdout="", stderr="", exit_code=1)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    refuse_empty_mirrors(ssh, tmp_path, ["dbt/models"], "/srv/dango/project", allow_empty=False)
    assert len(commands) == 1
    words = shlex.split(commands[0])
    assert words[:6] == ["runuser", "-u", "dango", "--", "sh", "-c"], words
    assert words[6].startswith("cd / && "), words[6]
    assert "find /srv/dango/project/dbt/models" in words[6]


@pytest.mark.unit
def test_missing_flock_error_names_flock_and_util_linux() -> None:
    from dango.platform.cloud import server_python_scripts as sps
    from dango.platform.cloud.server_python import ServerPythonError, install_pinned

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(
        stdout="", stderr="flock is not installed", exit_code=sps.EXIT_TOOLS
    )
    build = next(iter(sps.PYTHON_BUILDS.values()))
    with pytest.raises(ServerPythonError, match=r"flock \(util-linux\) is required"):
        install_pinned(ssh, build)


@pytest.mark.unit
def test_byos_setup_prints_the_failed_setup_step(tmp_path: Path) -> None:
    from dango.cli.commands import deploy_provision

    config = MagicMock(ssh_key_path="~/.ssh/k", server_ip="10.0.0.9", ssh_user="root", domain=None)
    failure = CloudProvisioningError("flock (util-linux) is required on the server")
    with (
        patch.object(deploy_provision, "_refuse_new_server"),
        patch("dango.platform.cloud.ssh.SSHManager"),
        patch("dango.platform.cloud.server_setup.resolve_install_source", return_value=None),
        patch("dango.platform.cloud.server_setup.setup_server", side_effect=failure),
        patch.object(deploy_provision.console, "print") as out,
        pytest.raises(CloudProvisioningError),
    ):
        deploy_provision.run_byos_setup(tmp_path, config, non_interactive=True)
    printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
    assert "flock (util-linux) is required on the server" in printed


@pytest.mark.unit
def test_push_with_a_changed_host_key_prints_one_clean_line(tmp_path: Path) -> None:
    from dango.cli.commands.remote import remote

    cfg = MagicMock(droplet_ip="10.0.0.1", deploy_branch=None)
    refusal = CloudSSHError("Host key verification failed for '10.0.0.1': the key has changed")
    with (
        patch("dango.cli.commands.remote._require_cloud_deployment", return_value=(cfg, tmp_path)),
        patch("dango.cli.commands.remote._connect_ssh", side_effect=refusal),
        patch("dango.platform.cloud.deployer.push_deploy") as push,
    ):
        result = CliRunner().invoke(remote, ["push", "--dry-run"], obj={})
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Failed to connect to server: Host key verification failed" in result.output
    push.assert_not_called()


@pytest.mark.unit
class TestDnsNote:
    @staticmethod
    def _printed(**kwargs: Any) -> str:
        from dango.cli.commands.deploy import _print_deploy_success

        with (
            patch("webbrowser.open"),
            patch("dango.cli.commands.deploy.console.print") as out,
        ):
            _print_deploy_success(
                url="https://app.example.com",
                ip="1.2.3.4",
                admin_email="a@example.com",
                warnings=[],
                **kwargs,
            )
        return "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)

    def test_domain_deploy_says_dns_first_and_gives_the_fix_command(self) -> None:
        text = self._printed(domain="app.example.com")
        assert "Point an A record for app.example.com to 1.2.3.4" in text
        assert "first" in text.lower()
        assert "systemctl restart caddy" in text
        assert text.index("A record") < text.index("systemctl restart caddy")

    def test_deploy_without_a_domain_has_no_dns_note(self) -> None:
        assert "systemctl restart caddy" not in self._printed(domain=None)
