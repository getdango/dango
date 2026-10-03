"""tests/unit/test_cloud_compose_project_name.py

Cloud upgrade and rebuild Compose commands must carry the server's Compose project name.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dango.platform.cloud.ssh import CommandResult

_PROJECT_YML_CMD = "cat /srv/dango/project/.dango/project.yml"
_PROJECT_YML = "project:\n  id: abcdef1234567890\n"


def _make_ssh(*, project_yml_ok: bool = True) -> tuple[MagicMock, list[str]]:
    """Return a mock SSHManager and the list recording every command it receives."""
    commands: list[str] = []

    def _exec(command: str, timeout: int | None = None) -> CommandResult:
        commands.append(command)
        if _PROJECT_YML_CMD in command:
            if project_yml_ok:
                return CommandResult(stdout=_PROJECT_YML, stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="no such file", exit_code=1)
        if "import dango" in command:
            return CommandResult(stdout="1.0.0", stderr="", exit_code=0)
        return CommandResult("", "", 0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    return ssh, commands


def _pull_command(commands: list[str]) -> str:
    matches = [c for c in commands if "pull" in c]
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.unit
def test_upgrade_compose_commands_carry_remote_project_name() -> None:
    from dango.platform.cloud.upgrade import upgrade_dango

    ssh, commands = _make_ssh()
    upgrade_dango(ssh, version="1.2.0", skip_backup=True)

    cmd = _pull_command(commands)
    assert cmd.count("COMPOSE_PROJECT_NAME=dango-abcdef12 docker compose") == 2
    assert cmd.endswith("up -d --build metabase </dev/null")


@pytest.mark.unit
def test_upgrade_falls_back_to_legacy_hash_when_project_yml_unreadable() -> None:
    from dango.platform.cloud.upgrade import upgrade_dango
    from dango.platform.docker import _legacy_path_hash

    ssh, commands = _make_ssh(project_yml_ok=False)
    upgrade_dango(ssh, version="1.2.0", skip_backup=True)

    expected = f"dango-{_legacy_path_hash('/srv/dango/project')}"
    assert f"COMPOSE_PROJECT_NAME={expected}" in _pull_command(commands)


@pytest.mark.unit
def test_upgrade_never_auto_confirms_volume_recreate() -> None:
    from dango.platform.cloud.upgrade import upgrade_dango

    ssh, commands = _make_ssh()
    upgrade_dango(ssh, version="1.2.0", skip_backup=True)

    assert "--yes" not in _pull_command(commands)


@pytest.mark.unit
def test_rebuild_down_carries_project_name_after_sudo() -> None:
    from dango.platform.cloud.deployer import _start_all_services

    ssh, commands = _make_ssh()
    _start_all_services(ssh, rebuild_docker=True)

    down = [c for c in commands if "down" in c and "docker compose" in c]
    assert len(down) == 1, down
    assert (
        "sudo -u dango env COMPOSE_PROJECT_NAME=dango-abcdef12 docker compose down --rmi local"
        in down[0]
    )


@pytest.mark.unit
def test_no_rebuild_issues_no_compose_command() -> None:
    from dango.platform.cloud.deployer import _start_all_services

    ssh, commands = _make_ssh()
    _start_all_services(ssh, rebuild_docker=False)

    assert not any("docker compose" in c for c in commands)
    assert any("systemctl restart dango-web" in c for c in commands)
