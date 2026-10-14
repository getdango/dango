"""tests/integration/test_rsync_hostkey_docker.py

rsync push refuses a server whose host key differs from the pinned one (two real sshd containers)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import paramiko
import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.file_sync import _rsync_directory

_IMAGE = "dango-test-rsync-sshd:1"
_DOCKERFILE = """\
FROM alpine:3.20
RUN apk add --no-cache openssh rsync && sed -i 's/^root:!/root:*/' /etc/shadow
CMD ["/bin/sh", "-c", "ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key \
&& mkdir -p /root/.ssh && echo \\"$AUTHORIZED_KEY\\" > /root/.ssh/authorized_keys \
&& chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys \
&& exec /usr/sbin/sshd -D -e -o HostKey=/etc/ssh/ssh_host_ed25519_key \
-o PasswordAuthentication=no -o PermitRootLogin=prohibit-password"]
"""


def _docker(*args: str, check: bool = True, input_: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, input=input_, timeout=300
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_key(port: int) -> paramiko.PKey:
    deadline = time.monotonic() + 30
    while True:
        try:
            transport = paramiko.Transport(("127.0.0.1", port))
            try:
                transport.start_client(timeout=5)
                return transport.get_remote_server_key()
            finally:
                transport.close()
        except (OSError, paramiko.SSHException):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="module")
def sshd_pair(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (needs Docker)")
    if shutil.which("docker") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("Docker and ssh-keygen are required")
    if shutil.which("rsync") is None:
        pytest.skip("rsync is required")

    work = tmp_path_factory.mktemp("rsync-hostkey")
    client_key = work / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
    )
    _docker("build", "-t", _IMAGE, "-", input_=_DOCKERFILE)

    names: list[str] = []
    ports: dict[str, int] = {}
    try:
        for role in ("real", "impostor"):
            name = f"dango-rsync-hostkey-{role}-{uuid.uuid4().hex[:8]}"
            port = _free_port()
            _docker(
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:22",
                "-e",
                f"AUTHORIZED_KEY={(work / 'id_ed25519.pub').read_text().strip()}",
                _IMAGE,
            )
            names.append(name)
            ports[role] = port

        real_key = _server_key(ports["real"])
        impostor_key = _server_key(ports["impostor"])
        assert real_key.asbytes() != impostor_key.asbytes(), "containers share a host key"

        # Pin the real server's key for BOTH addresses: the impostor answers on an address
        # whose pinned key it does not hold, as a man-in-the-middle would.
        known_hosts = work / "known_hosts"
        hk = paramiko.HostKeys()
        for port in ports.values():
            hk.add(f"[127.0.0.1]:{port}", real_key.get_name(), real_key)
        hk.save(str(known_hosts))

        yield {
            "names": dict(zip(("real", "impostor"), names, strict=True)),
            "ports": ports,
            "key": client_key,
            "known_hosts": known_hosts,
            "work": work,
        }
    finally:
        for name in names:
            _docker("rm", "-f", name, check=False)


@pytest.mark.integration
class TestRsyncHostKeyAgainstRealSshd:
    def _payload(self, work: Path, marker: str) -> Path:
        src = work / f"src-{marker}"
        src.mkdir()
        (src / "model.sql").write_text(f"SELECT '{marker}'")
        return src

    def test_rsync_impostor_refused_real_host_accepted(self, sshd_pair: dict) -> None:
        work = sshd_pair["work"]

        # Real host (non-22 port), pinned key matches: push succeeds.
        _rsync_directory(
            self._payload(work, "real"),
            "root@127.0.0.1:/srv/push",
            sshd_pair["key"],
            known_hosts_path=sshd_pair["known_hosts"],
            port=sshd_pair["ports"]["real"],
        )
        delivered = _docker("exec", sshd_pair["names"]["real"], "cat", "/srv/push/model.sql")
        assert delivered == "SELECT 'real'"

        # Impostor: different host key for a pinned address: push refused, nothing delivered.
        with pytest.raises(CloudProvisioningError) as excinfo:
            _rsync_directory(
                self._payload(work, "impostor"),
                "root@127.0.0.1:/srv/push",
                sshd_pair["key"],
                known_hosts_path=sshd_pair["known_hosts"],
                port=sshd_pair["ports"]["impostor"],
            )
        message = str(excinfo.value)
        assert "Host key verification failed" in message or "has changed" in message
        listing = _docker("exec", sshd_pair["names"]["impostor"], "ls", "/srv", check=False)
        assert "push" not in listing
