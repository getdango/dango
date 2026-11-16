"""dango/platform/cloud/ssh.py

SSH key management and remote execution for Dango cloud deployments.

Provides Ed25519 key generation, SSH connections via paramiko, command
execution, SFTP file transfer, and Trust-On-First-Use (TOFU) known hosts
management.

paramiko is a core dependency (``pip install getdango``) and is lazy-imported
to keep CLI startup fast.

Authentication
--------------
Keys are stored at ``~/.dango/ssh/`` by default (configurable via
``key_path``).  Private key: PEM format, chmod 600.  Public key: OpenSSH
format at ``key_path + ".pub"``, suitable for DigitalOcean API upload.

Known Hosts
-----------
Uses Trust-On-First-Use (TOFU): the first connection to a host with no pinned
key accepts and saves its host key (``~/.dango/known_hosts`` by default).
Later connections must present exactly a pinned key; a changed key or an
unpinned key type raises ``CloudSSHError`` with remediation steps.  Pins in
the pre-1.0.14 file ``<key dir>/known_hosts`` are honoured read-only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dango.exceptions import CloudError, CloudSSHError
from dango.logging import get_logger

_logger = get_logger(__name__)

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
            "paramiko is required for SSH operations. Reinstall with: pip install getdango"
        ) from None


def host_key_name(host: str, port: int = 22) -> str:
    """Return the known_hosts name for *host* (``[host]:port`` for non-22 ports, as OpenSSH)."""
    return host if port == 22 else f"[{host}]:{port}"


def ssh_argv_path(path: Path) -> str:
    """Return *path* for ssh's ``-i`` argument.

    OpenSSH checks the ``-i`` path literally but then %-expands it before
    loading, so no spelling of a path containing ``%`` works (neither ``%`` nor
    ``%%``): such paths are refused with a clear error instead of failing
    authentication obscurely.

    Raises:
        CloudSSHError: If the path contains a quote character (rsync's ``-e``
            embeds it quoted) or ``%``.
    """
    text = str(path)
    if '"' in text or "'" in text:
        raise CloudSSHError(f"SSH file path must not contain quote characters: {text}")
    if "%" in text:
        raise CloudSSHError(
            f"SSH key path must not contain '%' (ssh cannot load such a key file): {text}"
        )
    return text


def ssh_config_path(path: Path) -> str:
    """Return *path* for use inside a double-quoted ssh option value (``UserKnownHostsFile``).

    ``%`` is doubled because ssh expands ``%`` tokens there.  ``$`` is refused
    because ssh also expands ``${VAR}`` there and it cannot be escaped.

    Raises:
        CloudSSHError: If the path contains a quote character or ``$``.
    """
    text = str(path)
    if '"' in text or "'" in text:
        raise CloudSSHError(f"SSH file path must not contain quote characters: {text}")
    if "$" in text:
        raise CloudSSHError(
            f"SSH known_hosts path must not contain '$' (ssh expands ${{...}} in it): {text}"
        )
    return text.replace("%", "%%")


def _parse_known_hosts_line(pm: Any, line: str, lineno: int) -> Any:
    """Return a paramiko ``HostKeyEntry`` for *line*, or None if it cannot be used."""
    try:
        entry = pm.hostkeys.HostKeyEntry.from_line(line, lineno)
    except Exception:  # corrupt base64 / key data: skip the line, keep the rest
        return None
    return entry if entry is not None and entry.key is not None else None


@dataclass
class _KnownHosts:
    """Parsed known_hosts file: usable pins plus ``@revoked`` keys (as raw key bytes)."""

    host_keys: Any
    revoked: set[Any]


def _load_host_keys(pm: Any, path: Path, *, warn: bool = True) -> _KnownHosts:
    """Load *path* tolerantly, skipping lines paramiko cannot use.

    A corrupt line, a ``@cert-authority`` marker or an unsupported key type
    (common in a user's own ``~/.ssh/known_hosts``) is skipped with one warning
    naming the file and line numbers; every valid entry (plain or hashed) is
    kept, and ``@revoked`` keys are collected.  The file is never modified here.
    """
    parsed = _KnownHosts(host_keys=pm.HostKeys(), revoked=set())
    if not path.is_file():
        return parsed
    skipped: list[int] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(None, 1)  # markers are separated by any whitespace (OpenSSH)
        if fields[0] == "@revoked":
            rest = fields[1] if len(fields) > 1 else ""
            entry = _parse_known_hosts_line(pm, rest, lineno)
            if entry is None:
                skipped.append(lineno)
            else:
                parsed.revoked.add(entry.key.asbytes())
            continue
        entry = None if line.startswith("@") else _parse_known_hosts_line(pm, line, lineno)
        if entry is None:
            skipped.append(lineno)
            continue
        for name in entry.hostnames:
            parsed.host_keys.add(name, entry.key.get_name(), entry.key)
    if skipped and warn:
        _logger.warning("known_hosts_lines_skipped", path=str(path), lines=skipped)
    return parsed


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace *path* with *data* atomically (temp file in the same dir, mode 0600)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def _store_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` (never unlinked: flock locks inodes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


class _TOFUHostKeyPolicy:
    """Trust-On-First-Use host key policy.

    First connection to a host with no pinned key: accept and save the key.
    A host with ANY pinned key only accepts that exact key: a different key,
    or a key of a type that is not pinned, raises ``CloudSSHError``.

    Keys are stored in OpenSSH known_hosts format at ``known_hosts_path``
    (created if missing, mode 0600).  New pins are appended under a lock and
    every other line is kept verbatim.  ``@revoked`` keys in any file are never
    accepted.  *legacy_paths*
    (files earlier versions pinned into) are read, never written: their pins
    are honoured and copied into ``known_hosts_path`` on use; pins that
    disagree between files are treated as a changed key.
    """

    def __init__(self, known_hosts_path: Path, legacy_paths: Sequence[Path] = ()) -> None:
        self._path = known_hosts_path
        self._legacy_paths = list(legacy_paths)
        self._pm = _ensure_paramiko()
        primary = _load_host_keys(self._pm, self._path)
        legacy = [_load_host_keys(self._pm, p) for p in self._legacy_paths]
        self._host_keys: Any = primary.host_keys
        self._legacy_keys: list[Any] = [parsed.host_keys for parsed in legacy]
        self._revoked: set[Any] = set(primary.revoked).union(*(p.revoked for p in legacy))

    def _files(self) -> str:
        return ", ".join(str(p) for p in [self._path, *self._legacy_paths])

    def _refuse_revoked(self, hostname: str, key: Any) -> None:
        if key.asbytes() in self._revoked:
            raise CloudSSHError(
                f"Host key for {hostname!r} is marked @revoked in {self._files()}; "
                "refusing to connect."
            )

    def _changed(self, hostname: str, detail: str = "") -> CloudSSHError:
        files = self._files()
        return CloudSSHError(
            f"Host key verification failed for {hostname!r}: the key has "
            f"changed since the last connection{detail}.  If this is expected "
            f"(e.g. the server was reprovisioned), remove the old entry "
            f"from {files} and reconnect."
        )

    def _pinned(self, hostname: str) -> dict[str, Any]:
        """Return ``{key_type: key}`` pinned for *hostname* across all files."""
        merged: dict[str, Any] = {}
        for host_keys in [self._host_keys, *self._legacy_keys]:
            if hostname not in host_keys:
                continue
            for key_type, key in host_keys[hostname].items():
                existing = merged.get(key_type)
                if existing is not None and existing != key:
                    raise self._changed(hostname, " (known_hosts files disagree)")
                merged[key_type] = key
        return merged

    def _save_pins(self, hostname: str, keys: dict[str, Any], *, first_use: bool) -> None:
        """Append *keys* for *hostname* to the store, leaving every other line verbatim.

        Under the store lock the file is re-read, so pins added concurrently by
        another process are kept; a concurrent pin that disagrees is refused.
        """
        with _store_lock(self._path):
            current = _load_host_keys(self._pm, self._path, warn=False).host_keys
            existing = current[hostname] if hostname in current else {}
            for key_type, key in keys.items():
                if key_type in existing and existing[key_type] != key:
                    raise self._changed(hostname)
            new = {t: k for t, k in keys.items() if t not in existing}
            if first_use and existing and new:
                raise self._changed(hostname, " (pinned concurrently with another key type)")
            if not new:
                return
            data = self._path.read_bytes() if self._path.is_file() else b""
            if data and not data.endswith(b"\n"):
                data += b"\n"
            for key_type, key in new.items():
                data += f"{hostname} {key_type} {key.get_base64()}\n".encode()
            _write_atomic(self._path, data)
        for key_type, key in new.items():
            self._host_keys.add(hostname, key_type, key)

    def adopt_existing_pin(self, hostname: str) -> bool:
        """Copy legacy pins for *hostname* into ``known_hosts_path``.

        Returns:
            True if *hostname* has a pinned key; False if it has none.

        Raises:
            CloudSSHError: If the files pin different keys for the host.
        """
        pinned = self._pinned(hostname)
        for key in pinned.values():
            self._refuse_revoked(hostname, key)
        primary = self._host_keys[hostname] if hostname in self._host_keys else {}
        missing = {t: k for t, k in pinned.items() if t not in primary}
        if missing:
            self._save_pins(hostname, missing, first_use=False)
        return bool(pinned)

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        """Called by paramiko when the host key is not in known_hosts.

        Accepts and persists the key only for a host with no pinned key (TOFU).
        """
        _ensure_paramiko()
        key_type: str = key.get_name()
        self._refuse_revoked(hostname, key)
        pinned = self._pinned(hostname)
        if not pinned:
            self._save_pins(hostname, {key_type: key}, first_use=True)
            return
        stored = pinned.get(key_type)
        if stored is None:
            raise self._changed(
                hostname, f" (server offered {key_type}; pinned: {', '.join(sorted(pinned))})"
            )
        if stored != key:
            raise self._changed(hostname)
        # Key matches; carry a legacy-only pin over to the current file.
        self.adopt_existing_pin(hostname)


def ensure_host_pinned(known_hosts_path: Path, legacy_paths: Sequence[Path], hostname: str) -> bool:
    """Return whether *hostname* has a pinned key, copying legacy pins into *known_hosts_path*.

    Used before handing verification to OpenSSH (rsync, ``dango remote ssh``),
    which reads only *known_hosts_path*.

    Raises:
        CloudSSHError: If the known_hosts files pin different keys for the host.
    """
    return _TOFUHostKeyPolicy(known_hosts_path, legacy_paths).adopt_existing_pin(hostname)


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
        #: Endpoint of the last successful :meth:`connect` (used by rsync).
        self.host: str | None = None
        self.port: int = 22
        self.username: str = "root"

    @property
    def legacy_known_hosts_paths(self) -> list[Path]:
        """Existing pre-1.0.14 pin file (``<key dir>/known_hosts``), honoured read-only."""
        legacy = self.key_path.parent / "known_hosts"
        if not legacy.is_file() or legacy.resolve() == self.known_hosts_path.resolve():
            return []
        return [legacy]

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------

    def generate_key_pair(self) -> str:
        """Generate an Ed25519 key pair and write it to disk.

        Private key is written to ``key_path`` (PEM, no passphrase, mode
        0o600 set atomically to avoid a permissions race).  Public key is
        written to ``key_path + ".pub"`` in OpenSSH format.  Parent
        directories are created automatically.

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
                "It is a core dependency — reinstall with: pip install getdango"
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

            # Write with atomically correct permissions — avoids the TOCTOU
            # race between write_bytes() (0o644) and a subsequent chmod(0o600).
            fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)  # override umask
                os.write(fd, private_pem)
            finally:
                os.close(fd)

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
    # Internal connection helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        """Raise CloudSSHError if there is no active SSH connection."""
        if self._client is None:
            raise CloudSSHError("No active SSH connection.  Call connect() first.")

    def _load_key(self, pm: Any) -> Any:
        """Load the Ed25519 private key from disk.

        Args:
            pm: The paramiko module (from ``_ensure_paramiko``).

        Returns:
            A paramiko ``Ed25519Key`` ready for use in ``_connect_raw``.

        Raises:
            CloudSSHError: If the key file is missing or cannot be parsed.
        """
        try:
            return pm.Ed25519Key.from_private_key_file(str(self.key_path))
        except FileNotFoundError as exc:
            raise CloudSSHError(
                f"SSH private key not found at {self.key_path}.  Run generate_key_pair() first."
            ) from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"Failed to load SSH private key: {exc}") from exc

    def _connect_raw(self, pm: Any, host: str, username: str, port: int, key: Any) -> Any:
        """Open a TCP/SSH connection and return the paramiko SSHClient.

        Raises raw paramiko / OS exceptions — callers are responsible for
        wrapping them into ``CloudSSHError``.  A changed host key raises
        ``CloudSSHError`` directly from ``_TOFUHostKeyPolicy``.

        Args:
            pm: The paramiko module.
            host: Hostname or IP address.
            username: SSH username.
            port: SSH port.
            key: Pre-loaded paramiko ``Ed25519Key``.

        Returns:
            A connected paramiko ``SSHClient``.
        """
        client = pm.SSHClient()
        client.set_missing_host_key_policy(
            _TOFUHostKeyPolicy(self.known_hosts_path, self.legacy_known_hosts_paths)
        )
        client.connect(
            hostname=host,
            port=port,
            username=username,
            pkey=key,
            timeout=self.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        return client

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
        key = self._load_key(pm)  # raises CloudSSHError on missing / bad key
        try:
            client = self._connect_raw(pm, host, username, port, key)
        except pm.AuthenticationException as exc:
            raise CloudSSHError("SSH authentication failed: key rejected by host") from exc
        except TimeoutError as exc:
            raise CloudSSHError(f"SSH connection timed out after {self.connect_timeout}s") from exc
        except OSError as exc:
            raise CloudSSHError(f"SSH connection failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error: {exc}") from exc
        # CloudSSHError from _TOFUHostKeyPolicy (changed host key) propagates unchanged.

        self._client = client
        self.host = host
        self.port = port
        self.username = username
        return self

    def disconnect(self) -> None:
        """Close the SSH connection.  Idempotent — safe to call multiple times."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
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

        Permanent failures (missing key, bad key format, authentication
        rejection, changed host key) raise immediately without retrying.
        Only transient network errors (connection refused, timeout, SSH
        handshake not yet ready) are retried.

        Args:
            host: Hostname or IP address to connect to.
            username: SSH username.  Default: ``"root"``.
            port: SSH port.  Default: ``22``.
            timeout: Total time to wait in seconds.  Default: 120.
            interval: Initial sleep interval in seconds.  Default: 5.

        Raises:
            CloudSSHError: If SSH is not available before *timeout* expires,
                or immediately on a permanent configuration error.
            CloudError: If paramiko is not installed.
        """
        pm = _ensure_paramiko()
        # Load the key once and fail immediately on permanent config errors.
        key = self._load_key(pm)

        deadline = time.monotonic() + timeout
        sleep_interval = interval
        last_exc: Exception = CloudSSHError(
            f"SSH connection to {host}:{port} timed out after {timeout}s"
        )

        while time.monotonic() < deadline:
            try:
                client = self._connect_raw(pm, host, username, port, key)
                self._client = client
                return
            except pm.AuthenticationException as exc:
                # Permanent — do not retry.
                raise CloudSSHError("SSH authentication failed: key rejected by host") from exc
            except CloudSSHError:
                # From _TOFUHostKeyPolicy (changed host key) — permanent.
                raise
            except (TimeoutError, OSError, pm.SSHException) as exc:
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
            CloudSSHError: If not connected, if the command fails and *check*
                is ``True``, or if an SSH error occurs during execution.
        """
        self._require_connected()
        pm = _ensure_paramiko()
        effective_timeout = timeout if timeout is not None else self.command_timeout

        try:
            stdin, stdout_chan, stderr_chan = self._client.exec_command(
                command, timeout=effective_timeout
            )
            stdout_data = stdout_chan.read().decode("utf-8", errors="replace")
            stderr_data = stderr_chan.read().decode("utf-8", errors="replace")
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
        timeout: int | None = None,
    ) -> None:
        """Upload a local file to the remote host via SFTP.

        Args:
            local_path: Path to the local file to upload.
            remote_path: Destination path on the remote host.
            callback: Optional progress callback ``(bytes_transferred, total_bytes)``.
            timeout: Optional per-operation channel timeout in seconds.  If
                ``None``, no timeout is enforced on the transfer.

        Raises:
            CloudSSHError: If not connected or the SFTP transfer fails.
        """
        self._require_connected()
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                if timeout is not None:
                    sftp.get_channel().settimeout(timeout)
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
        timeout: int | None = None,
    ) -> None:
        """Download a file from the remote host via SFTP.

        Args:
            remote_path: Path to the file on the remote host.
            local_path: Destination path on the local filesystem.
            callback: Optional progress callback ``(bytes_transferred, total_bytes)``.
            timeout: Optional per-operation channel timeout in seconds.  If
                ``None``, no timeout is enforced on the transfer.

        Raises:
            CloudSSHError: If not connected or the SFTP transfer fails.
        """
        self._require_connected()
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                if timeout is not None:
                    sftp.get_channel().settimeout(timeout)
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
        timeout: int | None = None,
    ) -> None:
        """Write *content* to *remote_path* on the remote host via SFTP.

        Args:
            remote_path: Destination path on the remote host.
            content: String (encoded as UTF-8) or bytes to write.
            mode: File permission mode.  Default: ``0o644``.
            timeout: Optional per-operation channel timeout in seconds.  If
                ``None``, no timeout is enforced on the write.

        Raises:
            CloudSSHError: If not connected or the write fails.
        """
        self._require_connected()
        pm = _ensure_paramiko()
        data: bytes = content.encode("utf-8") if isinstance(content, str) else content
        try:
            sftp = self._client.open_sftp()
            try:
                if timeout is not None:
                    sftp.get_channel().settimeout(timeout)
                with sftp.open(remote_path, "wb") as remote_file:
                    # Pipelined writes avoid one round trip per block; a failed
                    # write is raised when the file closes.
                    remote_file.set_pipelined(True)
                    remote_file.write(data)
                sftp.chmod(remote_path, mode)
            finally:
                sftp.close()
        except OSError as exc:
            raise CloudSSHError(f"SFTP transfer failed: {exc}") from exc
        except pm.SSHException as exc:
            raise CloudSSHError(f"SSH error writing remote file: {exc}") from exc

    def read_remote_file(self, remote_path: str, timeout: int | None = None) -> str:
        """Read and return the contents of *remote_path* on the remote host.

        Args:
            remote_path: Path to the file on the remote host.
            timeout: Optional per-operation channel timeout in seconds.  If
                ``None``, no timeout is enforced on the read.

        Returns:
            File contents decoded as UTF-8.

        Raises:
            CloudSSHError: If not connected or the file cannot be read.
        """
        self._require_connected()
        pm = _ensure_paramiko()
        try:
            sftp = self._client.open_sftp()
            try:
                if timeout is not None:
                    sftp.get_channel().settimeout(timeout)
                with sftp.open(remote_path, "r") as remote_file:
                    return str(remote_file.read().decode("utf-8", errors="replace"))
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
        self._require_connected()
        return self._client.get_transport()
