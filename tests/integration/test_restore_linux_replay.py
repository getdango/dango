"""tests/integration/test_restore_linux_replay.py

Replays the server-restore shell commands as root inside a Linux container (GNU tar, runuser).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult

_B = "dango.platform.cloud.backup"
_IMAGE = "python:3.11-slim"
_TOP = "backup-20260101-000000"
_ARCHIVE_IN_CONTAINER = "/srv/dango/backups/deploy/restore-under-test.tar.gz"
#: Project layout on a real server, with data/ on separate (block) storage.
_SETUP = """
set -e
useradd -m dango
mkdir -p /srv/dango/project /srv/dango/backups/deploy /mnt/block /var/lib/mbvol
ln -s /mnt/block /srv/dango/project/data
chown -R dango:dango /srv/dango /mnt/block
chown -h dango:dango /srv/dango/project/data
echo 'root secret' > /root/target.txt
"""


class ContainerSSH:
    """``SSHManager`` stand-in running every command as root inside the container."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.commands: list[str] = []

    def exec_command(self, command: str, timeout: int = 60, **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        proc = subprocess.run(
            ["docker", "exec", self.name, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    def run(self, command: str) -> str:
        result = self.exec_command(command)
        assert result.success, result.stderr
        return result.stdout.strip()


@pytest.fixture
def container() -> Iterator[ContainerSSH]:
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
    if shutil.which("docker") is None:
        pytest.skip("Docker not available")
    name = f"dango-c3a-restore-replay-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, _IMAGE, "sleep", "600"],
        check=True,
        capture_output=True,
    )
    try:
        ssh = ContainerSSH(name)
        ssh.run(_SETUP)
        yield ssh
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _upload_archive(ssh: ContainerSSH, tmp_path: Path, content: Path) -> None:
    archive = tmp_path / "restore.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    subprocess.run(
        ["docker", "cp", str(archive), f"{ssh.name}:{_ARCHIVE_IN_CONTAINER}"],
        check=True,
        capture_output=True,
    )


def _restore(ssh: ContainerSSH) -> object:
    from dango.platform.cloud.backup import restore_from_archive

    with (
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value="/var/lib/mbvol"),
    ):
        return restore_from_archive(ssh, _ARCHIVE_IN_CONTAINER)


@pytest.mark.integration
def test_restore_on_linux_writes_as_service_user_and_keeps_data_symlink(
    container: ContainerSSH, tmp_path: Path
) -> None:
    content = tmp_path / "content"
    (content / "data").mkdir(parents=True)
    (content / "data" / "warehouse.duckdb").write_bytes(b"archive-warehouse")
    (content / ".dango").mkdir()
    (content / ".dango" / "project.yml").write_text("from-archive\n")
    (content / "dbt" / "models").mkdir(parents=True)
    (content / "dbt" / "models" / "m.sql").write_text("select 1\n")
    (content / "metabase").mkdir()
    (content / "metabase" / "metabase.db.mv.db").write_bytes(b"h2")
    (content / "manifest.json").write_text("{}")
    _upload_archive(container, tmp_path, content)

    _restore(container)

    run = container.run
    assert run("test -L /srv/dango/project/data && echo link") == "link"
    assert run("cat /mnt/block/warehouse.duckdb") == "archive-warehouse"
    assert run("cat /srv/dango/project/.dango/project.yml") == "from-archive"
    owners = run(
        "stat -c %U /mnt/block/warehouse.duckdb /srv/dango/project/.dango/project.yml"
        " /srv/dango/project/dbt/models/m.sql"
    ).split()
    assert owners == ["dango", "dango", "dango"]
    assert run("cat /var/lib/mbvol/metabase.db.mv.db") == "h2"
    assert run("ls /tmp | grep -c dango-restore-staging || true") == "0"
    # Every project-tree copy ran as the service user.
    project_copies = [
        c for c in container.commands if " cp " in f" {c}" and "/srv/dango/project" in c
    ]
    assert project_copies
    assert all("runuser -u dango -- cp" in c for c in project_copies), project_copies


@pytest.mark.integration
def test_restore_on_linux_refuses_symlink_member_before_any_write(
    container: ContainerSSH, tmp_path: Path
) -> None:
    content = tmp_path / "content"
    (content / ".dango").mkdir(parents=True)
    (content / ".dango" / "project.yml").write_text("from-archive\n")
    (content / ".dango" / "metabase.yml").symlink_to("/root/target.txt")
    (content / "manifest.json").write_text("{}")
    _upload_archive(container, tmp_path, content)

    with pytest.raises(CloudProvisioningError, match="link"):
        _restore(container)

    run = container.run
    assert run("cat /root/target.txt") == "root secret"
    assert run("test -e /srv/dango/project/.dango && echo yes || echo no") == "no"
    assert run("ls /tmp | grep -c dango-restore-staging || true") == "0"
    assert not any(c.startswith("/srv/dango/venv") for c in container.commands)
