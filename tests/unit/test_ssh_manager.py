"""tests/unit/test_ssh_manager.py

Unit tests for SSHManager key management, connection, and command execution
(dango/platform/cloud/ssh.py).

paramiko is injected via sys.modules patching — these tests run without
installing the [cloud] extra.  Follows the same pattern as test_spaces_client.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudSSHError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paramiko_mock() -> MagicMock:
    """Return a MagicMock wired to look like the paramiko module."""
    pm = MagicMock()

    # Exception classes — must be real Exception subclasses so except clauses work.
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

    # HostKeys
    pm.HostKeys.return_value = MagicMock()

    return pm


def _inject_paramiko(pm: MagicMock) -> Any:
    """Return a context manager that patches paramiko into sys.modules."""
    return patch.dict(sys.modules, {"paramiko": pm})


def _make_connected_manager(pm: MagicMock, tmp_path: Path) -> tuple[Any, MagicMock]:
    """Return (SSHManager with active mock connection, mock SSHClient)."""
    # Reset module-level cache so each test starts fresh.
    import dango.platform.cloud.ssh as ssh_mod

    ssh_mod._paramiko = None  # type: ignore[attr-defined]

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
# 1. CommandResult
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommandResult:
    def test_fields_stored(self):
        """CommandResult stores stdout, stderr, and exit_code."""
        from dango.platform.cloud.ssh import CommandResult

        result = CommandResult(stdout="hello\n", stderr="", exit_code=0)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_success_true_on_zero(self):
        """success property returns True when exit_code is 0."""
        from dango.platform.cloud.ssh import CommandResult

        assert CommandResult(stdout="", stderr="", exit_code=0).success is True

    def test_success_false_on_nonzero(self):
        """success property returns False when exit_code is non-zero."""
        from dango.platform.cloud.ssh import CommandResult

        assert CommandResult(stdout="", stderr="err", exit_code=1).success is False

    def test_frozen(self):
        """CommandResult is frozen (immutable)."""
        from dango.platform.cloud.ssh import CommandResult

        result = CommandResult(stdout="", stderr="", exit_code=0)
        with pytest.raises((AttributeError, TypeError)):
            result.exit_code = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. SSHManager constructor
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSSHManagerInit:
    def test_default_key_path(self):
        """Default key_path resolves to ~/.dango/ssh/dango_ed25519."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()
        assert manager.key_path == Path.home() / ".dango" / "ssh" / "dango_ed25519"

    def test_default_known_hosts_path(self):
        """Default known_hosts_path resolves to ~/.dango/known_hosts."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()
        assert manager.known_hosts_path == Path.home() / ".dango" / "known_hosts"

    def test_custom_paths(self, tmp_path: Path):
        """Custom key_path and known_hosts_path are stored as-is."""
        from dango.platform.cloud.ssh import SSHManager

        kp = tmp_path / "my_key"
        khp = tmp_path / "hosts"
        manager = SSHManager(key_path=kp, known_hosts_path=khp)
        assert manager.key_path == kp
        assert manager.known_hosts_path == khp

    def test_default_timeouts(self):
        """Default connect_timeout=30, command_timeout=60."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()
        assert manager.connect_timeout == 30
        assert manager.command_timeout == 60

    def test_custom_timeouts(self):
        """Custom timeouts are stored."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(connect_timeout=10, command_timeout=120)
        assert manager.connect_timeout == 10
        assert manager.command_timeout == 120

    def test_not_connected_initially(self):
        """is_connected returns False before connect() is called."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()
        assert manager.is_connected is False


# ---------------------------------------------------------------------------
# 3. Key generation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeyGeneration:
    def test_generate_creates_private_key(self, tmp_path: Path):
        """generate_key_pair() creates the private key file."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "ssh" / "id_ed25519")
        manager.generate_key_pair()

        assert manager.key_path.exists()

    def test_generate_creates_public_key(self, tmp_path: Path):
        """generate_key_pair() creates the public key file."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "ssh" / "id_ed25519")
        manager.generate_key_pair()

        pub_path = Path(str(manager.key_path) + ".pub")
        assert pub_path.exists()

    def test_generate_creates_parent_dirs(self, tmp_path: Path):
        """generate_key_pair() creates parent directories if missing."""
        from dango.platform.cloud.ssh import SSHManager

        nested = tmp_path / "a" / "b" / "c" / "id_ed25519"
        manager = SSHManager(key_path=nested)
        manager.generate_key_pair()

        assert nested.exists()

    def test_generate_private_key_permissions(self, tmp_path: Path):
        """Private key file has mode 600."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        manager.generate_key_pair()

        mode = manager.key_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_generate_returns_public_key_string(self, tmp_path: Path):
        """generate_key_pair() returns the public key as a string."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        public_key = manager.generate_key_pair()

        assert isinstance(public_key, str)
        assert public_key.startswith("ssh-ed25519")

    def test_key_pair_exists_both_present(self, tmp_path: Path):
        """key_pair_exists() returns True when both key files exist."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        manager.generate_key_pair()

        assert manager.key_pair_exists() is True

    def test_key_pair_exists_missing_private(self, tmp_path: Path):
        """key_pair_exists() returns False when private key is missing."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        manager.generate_key_pair()
        manager.key_path.unlink()  # remove private key

        assert manager.key_pair_exists() is False

    def test_key_pair_exists_missing_public(self, tmp_path: Path):
        """key_pair_exists() returns False when public key is missing."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        manager.generate_key_pair()
        pub = Path(str(manager.key_path) + ".pub")
        pub.unlink()

        assert manager.key_pair_exists() is False

    def test_get_public_key_returns_content(self, tmp_path: Path):
        """get_public_key() returns the content of the .pub file."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")
        generated = manager.generate_key_pair()

        assert manager.get_public_key() == generated

    def test_get_public_key_missing_raises(self, tmp_path: Path):
        """get_public_key() raises CloudSSHError when .pub file is absent."""
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "id_ed25519")

        with pytest.raises(CloudSSHError, match="not found"):
            manager.get_public_key()


# ---------------------------------------------------------------------------
# 4. Connection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConnection:
    def _reset_cache(self) -> None:
        import dango.platform.cloud.ssh as ssh_mod

        ssh_mod._paramiko = None  # type: ignore[attr-defined]

    def test_connect_success(self, tmp_path: Path):
        """connect() opens an SSHClient and stores it."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            result = manager.connect("10.0.0.1")

        assert result is manager
        ssh_client_mock.connect.assert_called_once()
        call_kwargs = ssh_client_mock.connect.call_args[1]
        assert call_kwargs["hostname"] == "10.0.0.1"
        assert call_kwargs["username"] == "root"
        assert call_kwargs["port"] == 22

    def test_connect_custom_params(self, tmp_path: Path):
        """connect() passes custom username and port."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            manager.connect("10.0.0.1", username="ubuntu", port=2222)

        call_kwargs = ssh_client_mock.connect.call_args[1]
        assert call_kwargs["username"] == "ubuntu"
        assert call_kwargs["port"] == 2222

    def test_connect_auth_failure_raises_cloud_ssh_error(self, tmp_path: Path):
        """AuthenticationException is wrapped as CloudSSHError."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()
        ssh_client_mock.connect.side_effect = pm.AuthenticationException("auth failed")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with pytest.raises(CloudSSHError, match="authentication failed"):
                manager.connect("10.0.0.1")

    def test_connect_socket_timeout_raises_cloud_ssh_error(self, tmp_path: Path):
        """socket.timeout is wrapped as CloudSSHError."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()
        ssh_client_mock.connect.side_effect = TimeoutError("timed out")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with pytest.raises(CloudSSHError, match="timed out"):
                manager.connect("10.0.0.1")

    def test_connect_missing_key_raises_cloud_ssh_error(self, tmp_path: Path):
        """FileNotFoundError for missing private key raises CloudSSHError."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        # Don't create the key file.
        pm.Ed25519Key.from_private_key_file.side_effect = FileNotFoundError("no such file")

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=tmp_path / "nonexistent", known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with pytest.raises(CloudSSHError, match="not found"):
                manager.connect("10.0.0.1")

    def test_disconnect_idempotent(self, tmp_path: Path):
        """disconnect() can be called multiple times without error."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, _ = _make_connected_manager(pm, tmp_path)

        manager.disconnect()
        manager.disconnect()  # should not raise

    def test_disconnect_clears_client(self, tmp_path: Path):
        """disconnect() sets _client to None."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, _ = _make_connected_manager(pm, tmp_path)

        manager.disconnect()

        assert manager._client is None

    def test_is_connected_true_when_transport_active(self, tmp_path: Path):
        """is_connected returns True when transport reports active."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        ssh_client_mock.get_transport.return_value.is_active.return_value = True
        assert manager.is_connected is True

    def test_is_connected_false_after_disconnect(self, tmp_path: Path):
        """is_connected returns False after disconnect."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, _ = _make_connected_manager(pm, tmp_path)

        manager.disconnect()
        assert manager.is_connected is False

    def test_context_manager_disconnects_on_exit(self, tmp_path: Path):
        """Context manager calls disconnect() on __exit__."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("private")

        ssh_client_mock = MagicMock()
        pm.SSHClient.return_value = ssh_client_mock
        pm.Ed25519Key.from_private_key_file.return_value = MagicMock()

        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager(key_path=key_path, known_hosts_path=tmp_path / "hosts")

        with _inject_paramiko(pm):
            with manager.connect("10.0.0.1"):
                pass  # exits here

        ssh_client_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# 5. exec_command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecCommand:
    def _reset_cache(self) -> None:
        import dango.platform.cloud.ssh as ssh_mod

        ssh_mod._paramiko = None  # type: ignore[attr-defined]

    def _make_exec_result(
        self,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Return (stdin, stdout_chan, stderr_chan) mocks."""
        stdin_mock = MagicMock()
        stdout_chan = MagicMock()
        stderr_chan = MagicMock()
        stdout_chan.read.return_value = stdout.encode("utf-8")
        stderr_chan.read.return_value = stderr.encode("utf-8")
        stdout_chan.channel.recv_exit_status.return_value = exit_code
        return stdin_mock, stdout_chan, stderr_chan

    def test_returns_command_result(self, tmp_path: Path):
        """exec_command() returns a CommandResult with correct fields."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        stdin, stdout_ch, stderr_ch = self._make_exec_result(
            stdout="hello\n", stderr="", exit_code=0
        )
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        from dango.platform.cloud.ssh import CommandResult

        result = manager.exec_command("echo hello")

        assert isinstance(result, CommandResult)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_default_timeout_used(self, tmp_path: Path):
        """exec_command() passes command_timeout to paramiko by default."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)
        manager.command_timeout = 45

        stdin, stdout_ch, stderr_ch = self._make_exec_result()
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        manager.exec_command("uptime")

        call_kwargs = ssh_client_mock.exec_command.call_args[1]
        assert call_kwargs["timeout"] == 45

    def test_custom_timeout_overrides(self, tmp_path: Path):
        """exec_command() passes custom timeout when provided."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        stdin, stdout_ch, stderr_ch = self._make_exec_result()
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        manager.exec_command("uptime", timeout=120)

        call_kwargs = ssh_client_mock.exec_command.call_args[1]
        assert call_kwargs["timeout"] == 120

    def test_check_false_returns_on_nonzero(self, tmp_path: Path):
        """check=False (default) returns CommandResult even on non-zero exit."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        stdin, stdout_ch, stderr_ch = self._make_exec_result(exit_code=1, stderr="error")
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        result = manager.exec_command("false")

        assert result.exit_code == 1
        assert result.success is False

    def test_check_true_raises_on_nonzero(self, tmp_path: Path):
        """check=True raises CloudSSHError when exit_code != 0."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        stdin, stdout_ch, stderr_ch = self._make_exec_result(exit_code=1, stderr="bad cmd")
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        with pytest.raises(CloudSSHError, match="exit code 1"):
            manager.exec_command("false", check=True)

    def test_check_true_succeeds_on_zero(self, tmp_path: Path):
        """check=True does not raise when exit_code is 0."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        stdin, stdout_ch, stderr_ch = self._make_exec_result(stdout="ok", exit_code=0)
        ssh_client_mock.exec_command.return_value = (stdin, stdout_ch, stderr_ch)

        result = manager.exec_command("true", check=True)

        assert result.exit_code == 0

    def test_ssh_exception_wrapped(self, tmp_path: Path):
        """paramiko.SSHException during exec is wrapped as CloudSSHError."""
        self._reset_cache()
        pm = _make_paramiko_mock()
        manager, ssh_client_mock = _make_connected_manager(pm, tmp_path)

        ssh_client_mock.exec_command.side_effect = pm.SSHException("pipe broken")

        with pytest.raises(CloudSSHError, match="SSH error"):
            manager.exec_command("uptime")

    def test_exec_command_raises_when_not_connected(self):
        """exec_command() raises CloudSSHError when not connected."""
        self._reset_cache()
        from dango.platform.cloud.ssh import SSHManager

        manager = SSHManager()

        with pytest.raises(CloudSSHError, match="No active SSH connection"):
            manager.exec_command("uptime")
