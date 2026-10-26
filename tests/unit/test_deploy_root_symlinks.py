"""tests/unit/test_deploy_root_symlinks.py

Deploy steps that run as root must not follow symlinks placed in the project directory (chown, writes, reads).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT = "/srv/dango/project"
_AS_DANGO = ("runuser -u dango", "sudo -u dango")


class SymlinkAwareSSH:
    """Fake root SSH session over a project tree where *links* are symlinks.

    Records every place where root would follow one of the symlinks:
    - an SFTP write (``write_remote_file``) to a symlinked path;
    - ``chown`` on a symlinked path without ``-h`` (or ``chown -R`` without an explicit ``-P``);
    - a shell redirection (``>`` / ``>>``) onto, or ``cat`` of, a symlinked path in a root command;
    - ``mkdir -p`` at or under a symlinked path in a root command.
    Commands that switch to the dango user first are not root operations.
    """

    def __init__(self, links: dict[str, str]):
        self.links = links
        self.followed: list[str] = []
        self.commands: list[str] = []
        self.sftp_writes: list[str] = []

    def connect(self, host: str, username: str = "root") -> None:
        """Pretend to connect."""

    def disconnect(self) -> None:
        """Pretend to disconnect."""

    def write_remote_file(self, path: str, content, mode: int = 0o644, timeout=None) -> None:
        self.sftp_writes.append(path)
        if path in self.links:
            self.followed.append(f"sftp write {path}")

    def exec_command(self, command: str, timeout=None, check: bool = False):
        self.commands.append(command)
        for part in re.split(r"&&|\|\||;|\|", command):
            self._check_part(part.strip())
        result = MagicMock()
        # Remote scripts are staged in a directory the server makes (mktemp -d).
        result.stdout = "/tmp/dango-admin.AbCd1234" if "mktemp -d" in command else ""
        result.stderr = ""
        result.exit_code = 0
        result.success = True
        return result

    def _check_part(self, part: str) -> None:
        if not part or part.startswith(_AS_DANGO):
            return
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if words and words[0] == "chown":
            flags = {w for w in words[1:] if w.startswith("-")}
            recursive = any("R" in f for f in flags)
            safe = any("P" in f for f in flags) if recursive else any("h" in f for f in flags)
            for w in words[1:]:
                if not safe and any(w == link or link.startswith(w + "/") for link in self.links):
                    self.followed.append(f"chown {w}")
        if words and words[0] == "mkdir":
            for w in words[1:]:
                if any(w == link or w.startswith(link + "/") for link in self.links):
                    self.followed.append(f"mkdir {w}")
        if words and words[0] == "cat":
            for w in words[1:]:
                if w in self.links:
                    self.followed.append(f"cat {w}")
        for target in re.findall(r">>?\s*(\S+)", part):
            if target in self.links:
                self.followed.append(f"redirect {target}")


def _links(*names: str) -> dict[str, str]:
    return {f"{_PROJECT}/{n}": "/etc/shadow" for n in names}


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dlt").mkdir()
    (tmp_path / ".dlt" / "secrets.toml").write_text("[sources]\n")
    (tmp_path / ".env").write_text("A=1\n")
    return tmp_path


@pytest.mark.unit
class TestRootDoesNotFollowProjectSymlinks:
    def test_push_secrets(self, project_root):
        from dango.cli.commands.deploy_provision import _push_secrets

        ssh = SymlinkAwareSSH(_links(".env", ".dlt/secrets.toml"))
        _push_secrets(ssh, project_root)
        assert ssh.followed == []

    def test_create_admin_and_enable_auth(self):
        from dango.cli.commands.deploy_provision import _create_admin_and_enable_auth

        ssh = SymlinkAwareSSH(_links(".env", ".dango/auth.yml", ".dango/state"))
        _create_admin_and_enable_auth(ssh, "admin@example.com", "Str0ng-and-l0ng-passw0rd!")
        assert ssh.followed == []

    def test_trigger_metabase_setup(self):
        from dango.cli.commands.deploy_provision import _trigger_metabase_setup

        ssh = SymlinkAwareSSH(_links(".env"))
        _trigger_metabase_setup(ssh, "admin@example.com")
        assert ssh.followed == []

    def test_setup_backups_env_write(self, project_root):
        from dango.cli.commands.deploy_provision import _ResourceTracker, _setup_backups
        from dango.cli.commands.deploy_wizard import WizardConfig

        cfg = WizardConfig(
            region="nyc3",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="admin@example.com",
            admin_password="x",
            skip_oauth=True,
            enable_backups=True,
            monthly_cost=29,
            spaces_access_key="AK",
            spaces_secret_key="SK",
        )
        ssh = SymlinkAwareSSH(_links(".env"))
        with patch("dango.platform.cloud.spaces.SpacesClient"):
            _setup_backups(ssh, MagicMock(), project_root, cfg, _ResourceTracker())
        assert ssh.followed == []

    def test_generate_cloud_profiles(self, project_root):
        from dango.cli.commands.deploy_provision import _generate_cloud_profiles

        ssh = SymlinkAwareSSH(_links("dbt/profiles.yml"))
        _generate_cloud_profiles(ssh, project_root, "s-2vcpu-4gb")
        assert ssh.followed == []

    def test_recursive_project_chown_states_no_follow(self, project_root):
        """Both deploy paths chown the whole project tree with an explicit -P."""
        ssh = _run_byos(project_root, _links("dbt/profiles.yml"))
        recursive = [c for c in ssh.commands if c.startswith("chown -R") or " chown -R" in c]
        assert recursive, "expected a recursive chown of the project"
        assert all(re.search(r"chown -R\S*P|chown -R -P", c) for c in recursive), recursive
        assert ssh.followed == []

    def test_dbt_target_created_as_dango(self, project_root):
        ssh = _run_byos(project_root, _links("dbt"))
        assert any("mkdir" in c and "dbt/target" in c for c in ssh.commands)
        assert ssh.followed == []


def _run_byos(project_root: Path, links: dict[str, str]) -> SymlinkAwareSSH:
    """Run the whole BYOS setup against a symlink-aware fake root session."""
    from dango.cli.commands.deploy_provision import run_byos_setup
    from dango.cli.commands.deploy_wizard import BYOSConfig

    ssh = SymlinkAwareSSH(links)
    cfg = BYOSConfig(
        server_ip="198.51.100.10",
        ssh_user="root",
        ssh_key_path=str(project_root / "key"),
        domain=None,
        admin_email="admin@example.com",
        admin_password="Str0ng-and-l0ng-passw0rd!",
        skip_oauth=True,
    )
    with (
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch(
            "dango.platform.cloud.server_setup.setup_server",
            return_value=MagicMock(warnings=[]),
        ),
        patch("dango.platform.cloud.server_setup.resolve_install_source", return_value="getdango"),
        patch("dango.platform.cloud.file_sync.sync_project_files"),
        patch("dango.cli.commands.deploy_provision._prebuild_images"),
        patch("dango.platform.cloud.backup.get_remote_compose_project_name", return_value="p"),
        patch("dango.cli.commands.deploy_provision._health_check", return_value=True),
        patch("dango.cli.commands.deploy_provision._wait_for_metabase", return_value=True),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
    ):
        run_byos_setup(project_root, cfg, non_interactive=True)
    return ssh
