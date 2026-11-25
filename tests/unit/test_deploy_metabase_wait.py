"""tests/unit/test_deploy_metabase_wait.py

Deploy and BYOS setup: Metabase gets the original total wait, and a slow first boot is reported.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_deploy_http_exposure_remote import _config
from tests.unit.test_deploy_root_symlinks import SymlinkAwareSSH

_PASSWORD = "Str0ng-and-l0ng-passw0rd!"
_TOTAL_METABASE_WAIT_SECONDS = 360
_WARNING = "Metabase is still starting"


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "sources.yml").write_text("sources: []")
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    return tmp_path


def _cmd_result(stdout: str = "") -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.exit_code = 0
    result.success = True
    return result


def _provision(project_root: Path, metabase_ready: bool) -> tuple[list[str], MagicMock]:
    """Run the DigitalOcean provisioning with everything external faked."""
    from dango.cli.commands.deploy_provision import run_provisioning

    ssh = MagicMock()
    ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA"
    ssh.exec_command.side_effect = lambda command, *a, **k: _cmd_result(
        "/tmp/dango-admin.AbCd1234\n" if "mktemp -d" in command else ""
    )
    client = MagicMock()
    client.upload_ssh_key.return_value = {"id": 111}
    config = _config([])
    wait = MagicMock(return_value=metabase_ready)
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch(
            "dango.platform.cloud.provisioning.provision_droplet",
            return_value={
                "id": 222,
                "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
            },
        ),
        patch(
            "dango.platform.cloud.firewall.create_default_firewall", return_value={"id": "fw-333"}
        ),
        patch(
            "dango.platform.cloud.server_setup.setup_server", return_value=MagicMock(warnings=[])
        ),
        patch("dango.platform.cloud.server_setup.resolve_install_source", return_value="getdango"),
        patch("dango.platform.cloud.file_sync.sync_project_files"),
        patch("dango.cli.commands.deploy_provision._prebuild_images"),
        patch("dango.platform.cloud.provisioning.save_provisioning_metadata"),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value="p"),
        patch("dango.cli.commands.deploy_provision._health_check", return_value=True),
        patch("dango.cli.commands.deploy_provision._wait_for_metabase", wait),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
    ):
        result = run_provisioning(project_root, config, non_interactive=True)
    return result.warnings, wait


def _byos(project_root: Path, metabase_ready: bool) -> tuple[list[str], MagicMock]:
    """Run the BYOS setup against a fake root session."""
    from dango.cli.commands.deploy_provision import run_byos_setup
    from dango.cli.commands.deploy_wizard import BYOSConfig

    # server_ip, ssh_user, ssh_key_path, domain, admin_email, admin_password, oauth flag
    config = BYOSConfig(
        "198.51.100.10",
        "root",
        str(project_root / "key"),
        None,
        "admin@example.com",
        _PASSWORD,
        True,
    )
    wait = MagicMock(return_value=metabase_ready)
    with (
        patch("dango.platform.cloud.ssh.SSHManager", return_value=SymlinkAwareSSH({})),
        patch(
            "dango.platform.cloud.server_setup.setup_server", return_value=MagicMock(warnings=[])
        ),
        patch("dango.platform.cloud.server_setup.resolve_install_source", return_value="getdango"),
        patch("dango.platform.cloud.file_sync.sync_project_files"),
        patch("dango.cli.commands.deploy_provision._prebuild_images"),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value="p"),
        patch("dango.cli.commands.deploy_provision._health_check", return_value=True),
        patch("dango.cli.commands.deploy_provision._wait_for_metabase", wait),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
    ):
        result = run_byos_setup(project_root, config, non_interactive=True)
    return result.warnings, wait


@pytest.mark.unit
@pytest.mark.parametrize(("run", "address"), [(_provision, "1.2.3.4"), (_byos, "198.51.100.10")])
class TestMetabaseWaitAtDeploy:
    def test_ready_metabase_adds_no_warning_and_gets_the_original_total_wait(
        self, project_root: Path, run, address: str
    ) -> None:
        warnings, wait = run(project_root, True)
        assert not any(_WARNING in w for w in warnings)
        wait.assert_called_once()
        assert wait.call_args.kwargs["timeout"] == _TOTAL_METABASE_WAIT_SECONDS

    def test_a_metabase_that_never_comes_up_is_reported_to_the_user(
        self, project_root: Path, run, address: str
    ) -> None:
        warnings, _wait = run(project_root, False)
        matching = [w for w in warnings if _WARNING in w]
        assert len(matching) == 1
        assert f"http://{address}" in matching[0]
