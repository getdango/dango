"""tests/unit/test_ssh_sftp.py

Unit tests for SSHManager SFTP operations, wait_for_ssh, known hosts
policy, and error wrapping (dango/platform/cloud/ssh.py).

paramiko is injected via sys.modules patching — these tests run without
installing the [cloud] extra.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError, CloudSSHError

# ---------------------------------------------------------------------------
# Helpers (duplicated from test_ssh_manager to keep test files self-contained)
# ---------------------------------------------------------------------------


def _make_paramiko_mock() -> MagicMock:
    """Return a MagicMock wired to look like the paramiko module."""
    pm = MagicMock()

    class _AuthenticationException(Exception):
        pass

    class _SSHException(Exception):
        pass

    class _ChannelException(Exception):
        def __init__(self, code: int = 0, text: str = "") -> None:
            self.code = code
            self.text = text
            super().__init__(text)

    pm.AuthenticationException = _AuthenticationException
    pm.SSHException = _SSHException
    pm.ChannelException = _ChannelException
    pm.HostKeys.return_value = MagicMock()

    return pm


def _inject_paramiko(pm: MagicMock) -> Any:
    return patch.dict(sys.modules, {"paramiko": pm})


def _reset_cache() -> None:
    import dango.platform.cloud.ssh as ssh_mod

    ssh_mod._paramiko = None  # type: ignore[attr-defined]


def _make_connected_manager(pm: MagicMock, tmp_path: Path) -> tuple[Any, MagicMock]:
    """Return (SSHManager with active mock connection, mock SSHClient)."""
    _reset_cache()

    from dango.platform.cloud.ssh import SSHManager

    key_path = tmp_path / "id_ed25519"
    key_path.write_text("fake-private-key")

    ssh_client_mock = MagicMock()
    transport_mock = MagicMock()
    transport_mock.is_active.return_value = True
    ssh_client_mock.get_transport.return_value = transport_mock
    pm.SSHClient.return_value = ssh_client_mock
    pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

    manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "known_hosts")

    with _inject_paramiko(pm):
        manager.connect("10.0.0.1")

    return manager, ssh_client_mock


# ---------------------------------------------------------------------------
# 1. SFTP upload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSFTPUpload:
    def test_upload_calls_put(self, tmp_path: Path):
        """upload_file() calls sftp.put() with the correct paths."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        ssh_client_mock.open_sftp.return_value = sftp_mock

        local = tmp_path / "backup.duckdb"
        local.write_bytes(b"db")

        manager.upload_file(local, "/remote/backup.duckdb")

        sftp_mock.put.assert_called_once_with(str(local), "/remote/backup.duckdb", callback=None)
        sftp_mock.close.assert_called_once()

    def test_upload_passes_callback(self, tmp_path: Path):
        """upload_file() passes the progress callback to sftp.put()."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        ssh_client_mock.open_sftp.return_value = sftp_mock

        local = tmp_path / "file.bin"
        local.write_bytes(b"data")
        cb = MagicMock()

        manager.upload_file(local, "/remote/file.bin", callback=cb)

        sftp_mock.put.assert_called_once_with(str(local), "/remote/file.bin", callback=cb)

    def test_upload_io_error_raises_cloud_ssh_error(self, tmp_path: Path):
        """IOError during sftp.put() is wrapped as CloudSSHError."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        sftp_mock.put.side_effect = OSError("disk full")
        ssh_client_mock.open_sftp.return_value = sftp_mock

        local = tmp_path / "f.bin"
        local.write_bytes(b"x")

        with pytest.raises(CloudSSHError, match="SFTP transfer failed"):
            manager.upload_file(local, "/remote/f.bin")

    def test_upload_raises_when_not_connected(self, tmp_path: Path):
        """upload_file() raises CloudSSHError when not connected."""
        _reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.upload_file(tmp_path / "file.bin", "/remote/file.bin")

    def test_upload_applies_timeout(self, tmp_path: Path):
        """upload_file() sets the SFTP channel timeout when provided."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        channel_mock = MagicMock()
        sftp_mock.get_channel.return_value = channel_mock
        ssh_client_mock.open_sftp.return_value = sftp_mock

        local = tmp_path / "file.bin"
        local.write_bytes(b"data")

        manager.upload_file(local, "/remote/file.bin", timeout=30)

        channel_mock.settimeout.assert_called_once_with(30)


# ---------------------------------------------------------------------------
# 2. SFTP download
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSFTPDownload:
    def test_download_calls_get(self, tmp_path: Path):
        """download_file() calls sftp.get() with the correct paths."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        ssh_client_mock.open_sftp.return_value = sftp_mock

        local = tmp_path / "downloaded.duckdb"

        manager.download_file("/remote/backup.duckdb", local)

        sftp_mock.get.assert_called_once_with("/remote/backup.duckdb", str(local), callback=None)
        sftp_mock.close.assert_called_once()

    def test_download_passes_callback(self, tmp_path: Path):
        """download_file() passes the progress callback to sftp.get()."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        ssh_client_mock.open_sftp.return_value = sftp_mock

        cb = MagicMock()
        manager.download_file("/remote/f.bin", tmp_path / "f.bin", callback=cb)

        sftp_mock.get.assert_called_once_with("/remote/f.bin", str(tmp_path / "f.bin"), callback=cb)

    def test_download_io_error_raises_cloud_ssh_error(self, tmp_path: Path):
        """IOError during sftp.get() is wrapped as CloudSSHError."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        sftp_mock.get.side_effect = OSError("no such file")
        ssh_client_mock.open_sftp.return_value = sftp_mock

        with pytest.raises(CloudSSHError, match="SFTP transfer failed"):
            manager.download_file("/remote/missing.bin", tmp_path / "out.bin")

    def test_download_raises_when_not_connected(self, tmp_path: Path):
        """download_file() raises CloudSSHError when not connected."""
        _reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.download_file("/remote/file.bin", tmp_path / "out.bin")


# ---------------------------------------------------------------------------
# 3. write_remote_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWriteRemoteFile:
    def test_string_encoded_as_utf8(self, tmp_path: Path):
        """write_remote_file() encodes str content as UTF-8."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        remote_file_mock = MagicMock()
        sftp_mock.open.return_value.__enter__ = lambda s: remote_file_mock
        sftp_mock.open.return_value.__exit__ = MagicMock(return_value=False)
        ssh_client_mock.open_sftp.return_value = sftp_mock

        manager.write_remote_file("/etc/config.conf", "key=value\n")

        remote_file_mock.write.assert_called_once_with(b"key=value\n")

    def test_bytes_written_directly(self, tmp_path: Path):
        """write_remote_file() writes bytes content without re-encoding."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        remote_file_mock = MagicMock()
        sftp_mock.open.return_value.__enter__ = lambda s: remote_file_mock
        sftp_mock.open.return_value.__exit__ = MagicMock(return_value=False)
        ssh_client_mock.open_sftp.return_value = sftp_mock

        manager.write_remote_file("/tmp/data.bin", b"\x00\x01\x02")

        remote_file_mock.write.assert_called_once_with(b"\x00\x01\x02")

    def test_chmod_called_with_mode(self, tmp_path: Path):
        """write_remote_file() calls sftp.chmod() with the given mode."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        remote_file_mock = MagicMock()
        sftp_mock.open.return_value.__enter__ = lambda s: remote_file_mock
        sftp_mock.open.return_value.__exit__ = MagicMock(return_value=False)
        ssh_client_mock.open_sftp.return_value = sftp_mock

        manager.write_remote_file("/etc/secret", "data", mode=0o600)

        sftp_mock.chmod.assert_called_once_with("/etc/secret", 0o600)

    def test_write_raises_when_not_connected(self):
        """write_remote_file() raises CloudSSHError when not connected."""
        _reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.write_remote_file("/remote/config.conf", "content")


# ---------------------------------------------------------------------------
# 4. read_remote_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadRemoteFile:
    def test_returns_decoded_contents(self, tmp_path: Path):
        """read_remote_file() returns file contents decoded as UTF-8."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        remote_file_mock = MagicMock()
        remote_file_mock.read.return_value = b"hello world\n"
        sftp_mock.open.return_value.__enter__ = lambda s: remote_file_mock
        sftp_mock.open.return_value.__exit__ = MagicMock(return_value=False)
        ssh_client_mock.open_sftp.return_value = sftp_mock

        result = manager.read_remote_file("/etc/config.conf")

        assert result == "hello world\n"

    def test_io_error_raises_cloud_ssh_error(self, tmp_path: Path):
        """IOError during sftp.open() raises CloudSSHError."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        sftp_mock = MagicMock()
        sftp_mock.open.side_effect = OSError("no such file")
        ssh_client_mock.open_sftp.return_value = sftp_mock

        with pytest.raises(CloudSSHError, match="SFTP transfer failed"):
            manager.read_remote_file("/nonexistent/file")

    def test_read_raises_when_not_connected(self):
        """read_remote_file() raises CloudSSHError when not connected."""
        _reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.read_remote_file("/remote/config.conf")


# ---------------------------------------------------------------------------
# 5. wait_for_ssh
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForSSH:
    def test_succeeds_on_first_attempt(self, tmp_path: Path):
        """wait_for_ssh() returns immediately when connect() succeeds."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with patch("time.sleep") as sleep_mock:
                manager.wait_for_ssh("10.0.0.1")

        sleep_mock.assert_not_called()
        assert manager.is_connected

    def test_succeeds_after_retries(self, tmp_path: Path):
        """wait_for_ssh() retries on SSHException and eventually connects."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        # First two calls fail, third succeeds.
        ssh_client_mock.connect.side_effect = [
            pm.SSHException("not ready"),
            pm.SSHException("not ready"),
            None,  # success
        ]

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with patch("time.sleep"):
                manager.wait_for_ssh("10.0.0.1", timeout=60)

        assert ssh_client_mock.connect.call_count == 3

    def test_timeout_raises_cloud_ssh_error(self, tmp_path: Path):
        """wait_for_ssh() raises CloudSSHError when timeout expires."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()
        ssh_client_mock.connect.side_effect = pm.SSHException("connection refused")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=[0.0, 200.0]):
                    with pytest.raises(CloudSSHError, match="timed out"):
                        manager.wait_for_ssh("10.0.0.1", timeout=120)

    def test_sleep_intervals_increase(self, tmp_path: Path):
        """wait_for_ssh() sleep intervals grow exponentially (capped at 15s)."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        # Fail 4 times, succeed on 5th.
        ssh_client_mock.connect.side_effect = [
            pm.SSHException(),
            pm.SSHException(),
            pm.SSHException(),
            pm.SSHException(),
            None,
        ]

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")
        sleep_calls: list[float] = []

        def record_sleep(s: float) -> None:
            sleep_calls.append(s)

        with _inject_paramiko(pm):
            with patch("time.sleep", side_effect=record_sleep):
                manager.wait_for_ssh("10.0.0.1", timeout=300, interval=5.0)

        assert len(sleep_calls) == 4
        # Each interval should be >= previous (exponential backoff).
        for i in range(1, len(sleep_calls)):
            assert sleep_calls[i] >= sleep_calls[i - 1]
        # All intervals capped at 15s.
        for s in sleep_calls:
            assert s <= 15.0

    def test_auth_failure_raises_immediately(self, tmp_path: Path):
        """wait_for_ssh() raises immediately on auth failure without retrying."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()
        ssh_client_mock.connect.side_effect = pm.AuthenticationException("auth rejected")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with patch("time.sleep") as sleep_mock:
                with pytest.raises(CloudSSHError, match="authentication failed"):
                    manager.wait_for_ssh("10.0.0.1", timeout=120)

        # Should fail fast — no sleep between retries.
        sleep_mock.assert_not_called()

    def test_key_not_found_raises_immediately(self, tmp_path: Path):
        """wait_for_ssh() raises immediately when the private key is missing."""
        _reset_cache()
        pm = _make_paramiko_mock()

        # Key file does NOT exist — _load_key() called before retry loop.
        pm.Ed25519Key.from_private_key_file.side_effect = FileNotFoundError("no key")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "missing_key", known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with patch("time.sleep") as sleep_mock:
                with pytest.raises(CloudSSHError, match="not found"):
                    manager.wait_for_ssh("10.0.0.1", timeout=120)

        sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Known hosts (TOFU policy)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnownHosts:
    def test_new_host_accepted_and_saved(self, tmp_path: Path):
        """First connection to a new host saves the key to known_hosts."""
        _reset_cache()
        import dango.platform.cloud.ssh as ssh_mod

        pm = _make_paramiko_mock()

        host_keys_mock = MagicMock()
        host_keys_mock.__contains__ = MagicMock(return_value=False)
        pm.HostKeys.return_value = host_keys_mock

        known_hosts = tmp_path / "known_hosts"
        # File does not yet exist.

        with _inject_paramiko(pm):
            policy = ssh_mod._TOFUHostKeyPolicy(known_hosts)
            key_mock = MagicMock()
            key_mock.get_name.return_value = "ssh-ed25519"
            client_mock = MagicMock()

            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            known_hosts.touch()  # create file so save() works

            policy.missing_host_key(client_mock, "10.0.0.1", key_mock)

        host_keys_mock.add.assert_called_once_with("10.0.0.1", "ssh-ed25519", key_mock)
        host_keys_mock.save.assert_called_once()

    def test_matching_key_accepted(self, tmp_path: Path):
        """Known host with matching key is accepted silently."""
        _reset_cache()
        import dango.platform.cloud.ssh as ssh_mod

        pm = _make_paramiko_mock()

        stored_key = MagicMock()
        stored_key_dict = {"ssh-ed25519": stored_key}
        host_keys_mock = MagicMock()
        host_keys_mock.__contains__ = MagicMock(return_value=True)
        host_keys_mock.__getitem__ = MagicMock(return_value=stored_key_dict)

        pm.HostKeys.return_value = host_keys_mock

        known_hosts = tmp_path / "known_hosts"
        known_hosts.touch()

        with _inject_paramiko(pm):
            policy = ssh_mod._TOFUHostKeyPolicy(known_hosts)
            incoming_key = MagicMock()
            incoming_key.get_name.return_value = "ssh-ed25519"
            # Same key object → __eq__ returns True by default for same mock.
            incoming_key.__eq__ = stored_key.__eq__ = lambda self, other: self is other
            # Make them the same object.
            incoming_key = stored_key

            client_mock = MagicMock()
            # Should not raise.
            policy.missing_host_key(client_mock, "10.0.0.1", incoming_key)

    def test_changed_key_raises_cloud_ssh_error(self, tmp_path: Path):
        """Changed host key raises CloudSSHError with remediation message."""
        _reset_cache()
        import dango.platform.cloud.ssh as ssh_mod

        pm = _make_paramiko_mock()

        stored_key = MagicMock()
        different_key = MagicMock()
        # Ensure they are not equal.
        stored_key.__eq__ = lambda self, other: False
        different_key.__eq__ = lambda self, other: False

        stored_key_dict = {"ssh-ed25519": stored_key}
        host_keys_mock = MagicMock()
        host_keys_mock.__contains__ = MagicMock(return_value=True)
        host_keys_mock.__getitem__ = MagicMock(return_value=stored_key_dict)

        pm.HostKeys.return_value = host_keys_mock

        known_hosts = tmp_path / "known_hosts"
        known_hosts.touch()

        with _inject_paramiko(pm):
            policy = ssh_mod._TOFUHostKeyPolicy(known_hosts)
            different_key.get_name = MagicMock(return_value="ssh-ed25519")
            client_mock = MagicMock()

            with pytest.raises(CloudSSHError, match="key has changed"):
                policy.missing_host_key(client_mock, "10.0.0.1", different_key)

    def test_known_hosts_file_created_if_missing(self, tmp_path: Path):
        """_TOFUHostKeyPolicy creates parent directories for known_hosts."""
        _reset_cache()
        import dango.platform.cloud.ssh as ssh_mod

        pm = _make_paramiko_mock()

        nested_hosts = tmp_path / "new" / "dir" / "known_hosts"
        # Parent does not exist.

        host_keys_mock = MagicMock()
        host_keys_mock.__contains__ = MagicMock(return_value=False)
        pm.HostKeys.return_value = host_keys_mock

        with _inject_paramiko(pm):
            policy = ssh_mod._TOFUHostKeyPolicy(nested_hosts)
            key_mock = MagicMock()
            key_mock.get_name.return_value = "ssh-ed25519"
            client_mock = MagicMock()

            # Create the file so save() can write.
            nested_hosts.parent.mkdir(parents=True, exist_ok=True)
            nested_hosts.touch()

            policy.missing_host_key(client_mock, "10.0.0.2", key_mock)

        assert nested_hosts.parent.exists()


# ---------------------------------------------------------------------------
# 7. Error wrapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorWrapping:
    def test_paramiko_missing_raises_cloud_error_with_hint(self):
        """CloudError with install hint raised when paramiko is not installed."""
        _reset_cache()

        import dango.platform.cloud.ssh as ssh_mod

        with patch.dict(sys.modules, {"paramiko": None}):  # type: ignore[dict-item]
            with pytest.raises(CloudError, match="pip install getdango\\[cloud\\]"):
                ssh_mod._ensure_paramiko()

    def test_paramiko_not_installed_reset_on_next_call(self):
        """_paramiko cache stays None after failed import."""
        _reset_cache()

        import dango.platform.cloud.ssh as ssh_mod

        with patch.dict(sys.modules, {"paramiko": None}):  # type: ignore[dict-item]
            try:
                ssh_mod._ensure_paramiko()
            except CloudError:
                pass

        # Cache should still be None (not poisoned with None).
        assert ssh_mod._paramiko is None  # type: ignore[attr-defined]

    def test_socket_error_in_connect_wrapped(self, tmp_path: Path):
        """socket.error during connect() is wrapped as CloudSSHError."""
        _reset_cache()
        pm = _make_paramiko_mock()

        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()
        ssh_client_mock.connect.side_effect = OSError("connection refused")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with pytest.raises(CloudSSHError, match="SSH connection failed"):
                manager.connect("10.0.0.1")

    def test_channel_exception_in_exec_wrapped(self, tmp_path: Path):
        """paramiko.ChannelException during exec_command is wrapped as CloudSSHError."""
        _reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        ssh_client_mock.exec_command.side_effect = pm.ChannelException(1, "channel closed")

        with pytest.raises(CloudSSHError, match="SSH channel error"):
            manager.exec_command("ls")

    def test_get_transport_no_connection_raises(self):
        """get_transport() raises CloudSSHError when not connected."""
        _reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.get_transport()
