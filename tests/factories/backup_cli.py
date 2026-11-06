"""tests/factories/backup_cli.py

Shared stand-ins for the CLI tests of off-site backup status, backup secrets and destructive-command reminders.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from dango.platform.cloud.ssh import CommandResult

LOADER = "dango.config.loader.ConfigLoader"
CTX = "dango.cli.utils.require_project_context"
SSH = "dango.platform.cloud.ssh.SSHManager"

DOWNLOAD_HINT = "dango remote backup download --from-server -o"
ENV_WITH_KEYS = "SPACES_ACCESS_KEY=ak\nSPACES_SECRET_KEY=sk\n"


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def make_cfg(
    *, spaces: bool = True, provider: str = "digitalocean", tmp_path: Path | None = None
) -> Any:
    cfg = MagicMock()
    cfg.provider = provider
    cfg.droplet_id = 42
    cfg.droplet_ip = "1.2.3.4"
    cfg.firewall_id = "fw-abc"
    cfg.ssh_key_id = 99
    cfg.ssh_key_path = ".dango/cloud_key"
    cfg.region = "nyc1"
    cfg.size = "s-2vcpu-4gb"
    cfg.domain = None
    if spaces:
        cfg.spaces = MagicMock()
        cfg.spaces.bucket = "my-bucket"
        cfg.spaces.region = "sgp1"
        cfg.spaces.access_key_env = "SPACES_ACCESS_KEY"
        cfg.spaces.secret_key_env = "SPACES_SECRET_KEY"
    else:
        cfg.spaces = None
    if tmp_path is not None:
        key = tmp_path / ".dango" / "cloud_key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("key")
    return cfg


def make_loader(cfg: Any) -> MagicMock:
    loader = MagicMock()
    loader.load_cloud_config.return_value = cfg
    return loader


def make_ssh(
    env_text: str = "",
    toml_text: str = "",
    *,
    run_stdout: str = "",
    timer: str = "active",
    version: str = "1.0.14",
) -> Any:
    """An SSH stand-in: answers the .env / secrets.toml reads, the timer check and anything else."""
    ssh = MagicMock()
    ssh.connect.return_value = ssh

    def exec_command(command: str, *_a: object, **_k: object) -> CommandResult:
        if "import dango" in command:  # the server's Dango version
            return CommandResult(stdout=version + "\n", stderr="", exit_code=0)
        if "is-active" in command:
            return CommandResult(
                stdout=timer + "\n", stderr="", exit_code=0 if timer == "active" else 3
            )
        if "secrets.toml" in command:
            return CommandResult(stdout=toml_text, stderr="", exit_code=0 if toml_text else 3)
        if ".env" in command and "sh -c" in command:
            return CommandResult(stdout=env_text, stderr="", exit_code=0 if env_text else 3)
        return CommandResult(stdout=run_stdout, stderr="", exit_code=0)

    ssh.exec_command.side_effect = exec_command
    return ssh


def invoke(group: Any, args: list[str], tmp_path: Path, cfg: Any, ssh: Any, **kw: Any) -> Any:
    with (
        patch(CTX, return_value=tmp_path),
        patch(LOADER, return_value=make_loader(cfg)),
        patch(SSH, return_value=ssh),
    ):
        return CliRunner().invoke(group, args, obj={"project_root": tmp_path}, **kw)
