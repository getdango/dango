"""dango/platform/cloud/ssh.py

SSH key management and remote execution for Dango cloud deployments.

Provides Ed25519 key generation, SSH connections via paramiko, command
execution, SFTP file transfer, and Trust-On-First-Use (TOFU) known hosts
management.

paramiko is an optional dependency (``pip install getdango[cloud]``) and is
lazy-imported so the core package can be installed without the cloud extras.

Authentication
--------------
Keys are stored at ``~/.dango/ssh/`` by default (configurable via
``key_path``).  Private key: PEM format, chmod 600.  Public key: OpenSSH
format at ``key_path + ".pub"``, suitable for DigitalOcean API upload.

Known Hosts
-----------
Uses Trust-On-First-Use (TOFU): the first connection to a new host accepts
and saves its host key.  Subsequent connections verify the key matches.  If
the key has changed, ``CloudSSHError`` is raised with remediation steps.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dango.exceptions import CloudError, CloudSSHError

# paramiko is lazy-imported via _ensure_paramiko().
# Module-level cache so the import only happens once.
_paramiko: Any = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_paramiko() -> Any:
    """Return the paramiko module, importing it on first call.

    Raises:
        CloudError: If paramiko is not installed.
    """
    global _paramiko
    if _paramiko is not None:
        return _paramiko
    try:
        import paramiko as _pm  # type: ignore[import]

        _paramiko = _pm
        return _paramiko
    except ImportError:
        raise CloudError(
            "paramiko is required for SSH operations. Install with: pip install getdango[cloud]"
        ) from None


class _TOFUHostKeyPolicy:
    """Trust-On-First-Use host key policy.

    First connection to a new host: accept and save the key.
    Subsequent connections: verify the key matches.  If changed, raise
    ``CloudSSHError`` with a clear remediation message.

    Keys are stored in paramiko ``HostKeys`` format at ``known_hosts_path``.
    The file is created automatically if it does not exist.
    """

    def __init__(self, known_hosts_path: Path) -> None:
        self._path = known_hosts_path
        pm = _ensure_paramiko()
        self._host_keys: Any = pm.HostKeys()
        if self._path.exists():
            self._host_keys.load(str(self._path))

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        """Called by paramiko when the host key is not in known_hosts.

        Accepts and persists the key (TOFU).
        """
        _ensure_paramiko()
        key_type: str = key.get_name()
        if hostname not in self._host_keys:
            self._host_keys.add(hostname, key_type, key)
        else:
            # Host is known but key type is new — check if any key matches.
            stored = self._host_keys[hostname]
            if key_type in stored:
                stored_key = stored[key_type]
                if stored_key != key:
                    raise CloudSSHError(
                        f"Host key verification failed for {hostname!r}: the key has "
                        f"changed since the last connection.  If this is expected "
                        f"(e.g. the server was reprovisioned), remove the old entry "
                        f"from {self._path} or pass reconnect=True."
                    )
                # Key matches — nothing to do.
                return
            # Different key type, add it.
            self._host_keys.add(hostname, key_type, key)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._host_keys.save(str(self._path))

    def check(self, hostname: str, key: Any) -> bool:
        """Return True if key matches the stored key for hostname.

        If the host is unknown, return False (TOFU will handle it via
        ``missing_host_key``).  If the host is known but the key has changed,
        raise ``CloudSSHError``.
        """
        pm = _ensure_paramiko()  # noqa: F841
        key_type: str = key.get_name()
        if hostname not in self._host_keys:
            return False
        stored = self._host_keys.get(hostname, {})
        if key_type not in stored:
            return False
        stored_key = stored[key_type]
        if stored_key != key:
            raise CloudSSHError(
                f"Host key verification failed for {hostname!r}: the key has "
                f"changed since the last connection.  If this is expected "
                f"(e.g. the server was reprovisioned), remove the old entry "
                f"from {self._path} or pass reconnect=True."
            )
        return True


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Result of a remote SSH command execution."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def success(self) -> bool:
        """Return True when the command exited with code 0."""
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# SSHManager
# ---------------------------------------------------------------------------


class SSHManager:
    """SSH key management and remote execution for Dango cloud deployments.

    Provides Ed25519 key pair generation, SSH connections with TOFU known
    hosts, remote command execution, and SFTP file transfer.

    Args:
        key_path: Path to the private key file.  The public key is stored at
            ``key_path + ".pub"``.  Defaults to ``~/.dango/ssh/dango_ed25519``.
        known_hosts_path: Path to the known hosts file (paramiko HostKeys
            format).  Defaults to ``~/.dango/known_hosts``.
        connect_timeout: TCP connection timeout in seconds.  Default: 30.
        command_timeout: Default command execution timeout in seconds.
            Default: 60.  Can be overridden per-call in ``exec_command()``.

    Example::

        from dango.platform.cloud import SSHManager

        ssh = SSHManager()
        ssh.generate_key_pair()         # creates ~/.dango/ssh/dango_ed25519{,.pub}
        public_key = ssh.get_public_key()

        with ssh.connect("203.0.113.10") as manager:
            result = manager.exec_command("uptime")
            print(result.stdout)
    """

    def __init__(
        self,
        key_path: Path | None = None,
        known_hosts_path: Path | None = None,
        connect_timeout: int = 30,
        command_timeout: int = 60,
    ) -> None:
        self.key_path: Path = key_path or Path.home() / ".dango" / "ssh" / "dango_ed25519"
        self.known_hosts_path: Path = known_hosts_path or Path.home() / ".dango" / "known_hosts"
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._client: Any = None

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def generate_key_pair(self) -> str:
        """Generate an Ed25519 key pair and write it to disk.

        Private key is written to ``key_path`` (PEM, no passphrase, chmod
        600).  Public key is written to ``key_path + ".pub"`` in OpenSSH
        format.  Parent directories are created automatically.

        Returns:
            The public key string (OpenSSH format, one line).

        Raises:
            CloudSSHError: If key generation or file I/O fails.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                PublicFormat,
            )
        except ImportError as exc:
            raise CloudSSHError(
                "cryptography package is required for key generation. "
                "Install with: pip install getdango[cloud]"
            ) from exc

        try:
            private_key = Ed25519PrivateKey.generate()
            private_pem = private_key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.OpenSSH,
                encryption_algorithm=NoEncryption(),
            )
            public_openssh = private_key.public_key().public_bytes(
                encoding=Encoding.OpenSSH,
                format=PublicFormat.OpenSSH,
            )

            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(private_pem)
            os.chmod(self.key_path, 0o600)

            pub_path = Path(str(self.key_path) + ".pub")
            pub_path.write_bytes(public_openssh)

            return public_openssh.decode("utf-8").strip()
        except OSError as exc:
            raise CloudSSHError(f"Failed to write SSH key files: {exc}") from exc

    def get_public_key(self) -> str:
        """Read and return the public key from disk.

        Returns:
            The public key string (OpenSSH format, one line).

        Raises:
            CloudSSHError: If the public key file does not exist.
        """
        pub_path = Path(str(self.key_path) + ".pub")
        if not pub_path.exists():
            raise CloudSSHError(
                f"SSH public key not found at {pub_path}.  Run generate_key_pair() first."
            )
        return pub_path.read_text(encoding="utf-8").strip()

    def key_pair_exists(self) -> bool:
        """Return True if both the private and public key files exist."""
        pub_path = Path(str(self.key_path) + ".pub")
        return self.key_path.exists() and pub_path.exists()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self, host: str, username: str = "root", port: int = 22) -> SSHManager:
        """Open an SSH connection to *host* using the stored Ed25519 key.

        Uses TOFU known-hosts policy: the first connection to a new host
        accepts and saves the host key.

        Args:
            host: Hostname or IP address to connect to.
            username: SSH username.  Default: ``"root"``.
            port: SSH port.  Default: ``22``.

        Returns:
            ``self`` (enables ``with ssh.connect(...) as manager:``).

        Raises:
            CloudSSHError: On authentication failure, timeout, or other SSH
                errors.
            CloudError: If paramiko is not installed.
        """
        pm = _ensure_paramiko()
        try:
            key = pm.Ed25519Key.from_private_key_file(str(self.key_path))
        except FileNotFoundError as exc:
            raise CloudSSHError(
                f"SSH private key not found at {self.key_path}.  Run generate_key_pair() first."
            ) from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"Failed to load SSH private key: {exc}") from exc

        client = pm.SSHClient()
        client.set_missing_host_key_policy(_TOFUHostKeyPolicy(self.known_hosts_path))

        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                pkey=key,
                timeout=self.connect_timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except pm.AuthenticationException as exc:
            raise CloudSSHError("SSH authentication failed: key rejected by host") from exc
        except TimeoutError as exc:
            raise CloudSSHError(f"SSH connection timed out after {self.connect_timeout}s") from exc
        except OSError as exc:
            raise CloudSSHError(f"SSH connection failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error: {exc}") from exc

        self._client = client
        return self

    def disconnect(self) -> None:
        """Close the SSH connection.  Idempotent — safe to call multiple times."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def is_connected(self) -> bool:
        """Return True if an active SSH transport exists."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def __enter__(self) -> SSHManager:
        """Return self for use as a context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Disconnect on context manager exit."""
        self.disconnect()

    # ------------------------------------------------------------------
    # Wait for SSH availability
    # ------------------------------------------------------------------

    def wait_for_ssh(
        self,
        host: str,
        username: str = "root",
        port: int = 22,
        timeout: int = 120,
        interval: float = 5.0,
    ) -> None:
        """Retry until SSH is available on *host*, then leave the connection open.

        Uses an exponential back-off strategy: initial interval of *interval*
        seconds, multiplied by 1.5 on each retry, capped at 15 seconds.

        Args:
            host: Hostname or IP address to connect to.
            username: SSH username.  Default: ``"root"``.
            port: SSH port.  Default: ``22``.
            timeout: Total time to wait in seconds.  Default: 120.
            interval: Initial sleep interval in seconds.  Default: 5.

        Raises:
            CloudSSHError: If SSH is not available before *timeout* expires.
            CloudError: If paramiko is not installed.
        """
        pm = _ensure_paramiko()
        deadline = time.monotonic() + timeout
        sleep_interval = interval
        last_exc: Exception = CloudSSHError(
            f"SSH connection to {host}:{port} timed out after {timeout}s"
        )

        while time.monotonic() < deadline:
            try:
                self.connect(host=host, username=username, port=port)
                return
            except (TimeoutError, pm.SSHException, OSError) as exc:
                last_exc = exc
            except CloudSSHError as exc:
                last_exc = exc

            time.sleep(min(sleep_interval, 15.0))
            sleep_interval *= 1.5

        raise CloudSSHError(
            f"SSH connection to {host}:{port} timed out after {timeout}s"
        ) from last_exc

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def exec_command(
        self,
        command: str,
        timeout: int | None = None,
        check: bool = False,
    ) -> CommandResult:
        """Execute *command* on the remote host and return a ``CommandResult``.

        Args:
            command: Shell command to run on the remote host.
            timeout: Command execution timeout in seconds.  Defaults to
                ``self.command_timeout``.
            check: If ``True``, raise ``CloudSSHError`` when the exit code is
                non-zero (mirrors ``subprocess.run(check=True)``).

        Returns:
            ``CommandResult`` with stdout, stderr, and exit_code.

        Raises:
            CloudSSHError: If the command fails and *check* is ``True``, or
                if an SSH error occurs during execution.
        """
        pm = _ensure_paramiko()
        effective_timeout = timeout if timeout is not None else self.command_timeout

        try:
            stdin, stdout_chan, stderr_chan = self._client.exec_command(
                command, timeout=effective_timeout
            )
            stdout_data = stdout_chan.read().decode("utf-8")
            stderr_data = stderr_chan.read().decode("utf-8")
            exit_code: int = stdout_chan.channel.recv_exit_status()
        except pm.ChannelException as exc:
            raise CloudSSHError(f"SSH channel error: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error: {exc}") from exc
        except OSError as exc:
            raise CloudSSHError(f"SSH connection failed: {exc}") from exc

        result = CommandResult(
            stdout=stdout_data,
            stderr=stderr_data,
            exit_code=exit_code,
        )

        if check and not result.success:
            raise CloudSSHError(
                f"Command failed with exit code {exit_code}: {command!r}\n"
                f"stderr: {stderr_data.strip()}"
            )

        return result

    # ------------------------------------------------------------------
    # SFTP file transfer
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path | str,
        remote_path: str,
        callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Upload a local file to the remote host via SFTP.

        Args:
            local_path: Path to the local file to upload.
            remote_path: Destination path on the remote host.
            callback: Optional progress callback ``(bytes_transferred, total_bytes)``.

        Raises:
            CloudSSHError: If the SFTP transfer fails.
        """
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                sftp.put(str(local_path), remote_path, callback=callback)
            finally:
                sftp.close()
        except OSError as exc:
            raise CloudSSHError(f"SFTP transfer failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error during SFTP upload: {exc}") from exc

    def download_file(
        self,
        remote_path: str,
        local_path: Path | str,
        callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Download a file from the remote host via SFTP.

        Args:
            remote_path: Path to the file on the remote host.
            local_path: Destination path on the local filesystem.
            callback: Optional progress callback ``(bytes_transferred, total_bytes)``.

        Raises:
            CloudSSHError: If the SFTP transfer fails.
        """
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                sftp.get(remote_path, str(local_path), callback=callback)
            finally:
                sftp.close()
        except OSError as exc:
            raise CloudSSHError(f"SFTP transfer failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error during SFTP download: {exc}") from exc

    def write_remote_file(
        self,
        remote_path: str,
        content: str | bytes,
        mode: int = 0o644,
    ) -> None:
        """Write *content* to *remote_path* on the remote host via SFTP.

        Args:
            remote_path: Destination path on the remote host.
            content: String (encoded as UTF-8) or bytes to write.
            mode: File permission mode.  Default: ``0o644``.

        Raises:
            CloudSSHError: If the write fails.
        """
        pm = _ensure_paramiko()
        data: bytes = content.encode("utf-8") if isinstance(content, str) else content
        try:
            sftp = self._client.open_sftp()
            try:
                with sftp.open(remote_path, "wb") as remote_file:
                    remote_file.write(data)
                sftp.chmod(remote_path, mode)
            finally:
                sftp.close()
        except OSError as exc:
            raise CloudSSHError(f"SFTP transfer failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error writing remote file: {exc}") from exc

    def read_remote_file(self, remote_path: str) -> str:
        """Read and return the contents of *remote_path* on the remote host.

        Args:
            remote_path: Path to the file on the remote host.

        Returns:
            File contents decoded as UTF-8.

        Raises:
            CloudSSHError: If the file cannot be read.
        """
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                with sftp.open(remote_path, "r") as remote_file:
                    return str(remote_file.read().decode("utf-8"))
            finally:
                sftp.close()
        except OSError as exc:
            raise CloudSSHError(f"SFTP transfer failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error reading remote file: {exc}") from exc

    # ------------------------------------------------------------------
    # Advanced / escape hatch
    # ------------------------------------------------------------------

    def get_transport(self) -> Any:
        """Expose the underlying paramiko ``Transport`` for advanced use.

        Returns:
            The active paramiko ``Transport`` object.

        Raises:
            CloudSSHError: If there is no active connection.
        """
        if self._client is None:
            raise CloudSSHError("No active SSH connection.  Call connect() first.")
        return self._client.get_transport()
