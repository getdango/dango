"""tests/unit/test_file_sync_hostkey.py

rsync and ``remote ssh`` verify the server host key pinned by SSHManager."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko
import pytest
from click.testing import CliRunner

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import file_sync
from dango.platform.cloud.file_sync import (
    _build_rsync_ssh_arg,
    _rsync_directory,
    sync_project_files,
)
from dango.platform.cloud.ssh import CommandResult, SSHManager

_HOST = "203.0.113.10"


def _pin(known_hosts: Path, name: str, key: paramiko.PKey | None = None) -> paramiko.PKey:
    """Write a known_hosts entry for *name* the way SSHManager's TOFU policy does."""
    hk = paramiko.HostKeys()
    if known_hosts.exists():
        hk.load(str(known_hosts))
    key = key or paramiko.ECDSAKey.generate()
    hk.add(name, key.get_name(), key)
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    hk.save(str(known_hosts))
    return key


def _pinned_keys(known_hosts: Path, name: str) -> list[bytes]:
    if not known_hosts.exists():
        return []
    hk = paramiko.HostKeys(str(known_hosts))
    entry = hk.lookup(name)
    return [k.asbytes() for k in entry.values()] if entry else []


def _ssh_mock(
    known_hosts: Path,
    *,
    host: str = _HOST,
    port: int = 22,
    legacy: list[Path] | None = None,
) -> MagicMock:
    ssh = MagicMock()
    ssh.key_path = known_hosts.parent / "id_ed25519"
    ssh.known_hosts_path = known_hosts
    ssh.legacy_known_hosts_paths = legacy or []
    ssh.username = "root"
    ssh.host = host
    ssh.port = port
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    ssh.upload_file.return_value = None
    return ssh


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "sources.yml").write_text("sources: []")
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "models" / "stg_a.sql").write_text("SELECT 1")
    return root


def _rsync_e_args(mock_run: MagicMock) -> list[str]:
    out = []
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        if cmd and cmd[0] == "rsync":
            out.append(cmd[cmd.index("-e") + 1])
    return out


@pytest.mark.unit
class TestRsyncSshArg:
    def test_rsync_ssh_arg_never_disables_host_checking(self, tmp_path: Path) -> None:
        kh = tmp_path / "known_hosts"
        for port in (22, 2222):
            arg = _build_rsync_ssh_arg(tmp_path / "key", known_hosts_path=kh, port=port)
            assert "StrictHostKeyChecking=no" not in arg
            assert "/dev/null" not in arg
            assert "StrictHostKeyChecking=yes" in arg
            assert "BatchMode=yes" in arg
        source = inspect.getsource(file_sync)
        assert "StrictHostKeyChecking=no" not in source
        assert "UserKnownHostsFile=/dev/null" not in source

    def test_rsync_ssh_arg_uses_manager_known_hosts_and_port(self, tmp_path: Path) -> None:
        kh = tmp_path / "dir with space" / "known_hosts"
        arg = _build_rsync_ssh_arg(tmp_path / "key", known_hosts_path=kh, port=2222)
        assert f'UserKnownHostsFile="{kh}"' in arg
        assert "-p 2222" in arg
        assert f'-i "{tmp_path / "key"}"' in arg

    @pytest.mark.parametrize("bad", ['/tmp/a"b/known_hosts', '/tmp/x"'])
    def test_rsync_ssh_arg_rejects_double_quote_in_paths(self, bad: str) -> None:
        with pytest.raises(CloudProvisioningError):
            _build_rsync_ssh_arg(Path("/tmp/key"), known_hosts_path=Path(bad), port=22)
        with pytest.raises(CloudProvisioningError):
            _build_rsync_ssh_arg(Path(bad), known_hosts_path=Path("/tmp/kh"), port=22)


@pytest.mark.unit
class TestSyncThreadsPinnedHost:
    @pytest.mark.parametrize(("port", "pinned_name"), [(22, _HOST), (2222, f"[{_HOST}]:2222")])
    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_sync_uses_manager_known_hosts_and_port(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path, port: int, pinned_name: str
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        kh = tmp_path / "ssh" / "known_hosts"
        _pin(kh, pinned_name)
        ssh = _ssh_mock(kh, port=port)

        sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)

        e_args = _rsync_e_args(mock_run)
        assert e_args, "rsync was not invoked"
        for arg in e_args:
            assert f'UserKnownHostsFile="{kh}"' in arg
            assert f"-p {port}" in arg
            assert "StrictHostKeyChecking=yes" in arg

    @pytest.mark.parametrize(
        "pinned",
        [None, "198.51.100.7", _HOST],  # missing file, other host, wrong port (2222)
    )
    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_rsync_refuses_unpinned_host(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path, pinned: str | None
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        kh = tmp_path / "ssh" / "known_hosts"
        if pinned is not None:
            _pin(kh, pinned)
        ssh = _ssh_mock(kh, port=2222 if pinned == _HOST else 22)

        with pytest.raises(CloudProvisioningError, match="not pinned"):
            sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)
        assert _rsync_e_args(mock_run) == []

    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_rsync_directory_refuses_unpinned_host(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        kh = tmp_path / "known_hosts"
        kh.write_text("")
        with pytest.raises(CloudProvisioningError, match="not pinned"):
            _rsync_directory(
                tmp_path,
                f"root@{_HOST}:/srv/x",
                tmp_path / "key",
                known_hosts_path=kh,
                port=22,
            )
        mock_run.assert_not_called()


@pytest.mark.unit
class TestSSHManagerRecordsEndpoint:
    def test_connect_records_host_and_port(self, tmp_path: Path) -> None:
        manager = SSHManager(key_path=tmp_path / "k", known_hosts_path=tmp_path / "kh")
        assert manager.host is None
        assert manager.port == 22
        assert manager.username == "root"
        with (
            patch.object(SSHManager, "_load_key", return_value=MagicMock()),
            patch.object(SSHManager, "_connect_raw", return_value=MagicMock()),
        ):
            manager.connect(_HOST, username="deploy", port=2222)
        assert manager.host == _HOST
        assert manager.port == 2222
        assert manager.username == "deploy"

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_rsync_uses_connected_username(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        kh = tmp_path / "known_hosts"
        _pin(kh, _HOST)
        ssh = _ssh_mock(kh)
        ssh.username = "deploy"

        sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)

        dests = [c[0][0][-1] for c in mock_run.call_args_list if c[0][0][0] == "rsync"]
        assert dests
        assert all(d.startswith(f"deploy@{_HOST}:") for d in dests)


@pytest.mark.unit
class TestLegacyKnownHostsForRsync:
    """Hosts pinned before 1.0.14 in ``<key dir>/known_hosts`` keep working without re-TOFU."""

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_host_pinned_only_in_legacy_file_pushes(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        primary = tmp_path / "home" / ".dango" / "known_hosts"
        legacy = tmp_path / "old-project" / ".dango" / "known_hosts"
        key = _pin(legacy, _HOST)
        ssh = _ssh_mock(primary, legacy=[legacy])

        sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)

        assert _pinned_keys(primary, _HOST) == [key.asbytes()]  # trusted pin carried over
        e_args = _rsync_e_args(mock_run)
        assert e_args
        for arg in e_args:
            assert f'UserKnownHostsFile="{primary}"' in arg
            assert "StrictHostKeyChecking=yes" in arg

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_conflicting_pins_refused(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        primary = tmp_path / "home" / ".dango" / "known_hosts"
        legacy = tmp_path / "old-project" / ".dango" / "known_hosts"
        _pin(primary, _HOST)
        _pin(legacy, _HOST)  # same key type, different key
        ssh = _ssh_mock(primary, legacy=[legacy])

        with pytest.raises(CloudProvisioningError, match="changed"):
            sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)
        assert _rsync_e_args(mock_run) == []

    @patch("dango.platform.cloud.file_sync.shutil.which", return_value="/usr/bin/rsync")
    @patch("dango.platform.cloud.rsync_transport.subprocess.run")
    def test_remote_host_must_match_connected_host(
        self, mock_run: MagicMock, _which: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        primary = tmp_path / "known_hosts"
        _pin(primary, _HOST)
        _pin(primary, "198.51.100.7")
        ssh = _ssh_mock(primary, host="198.51.100.7")

        with pytest.raises(CloudProvisioningError):
            sync_project_files(ssh, _project(tmp_path), remote_host=_HOST)
        assert _rsync_e_args(mock_run) == []

    def test_manager_lists_existing_legacy_file_only(self, tmp_path: Path) -> None:
        key_path = tmp_path / "project" / ".dango" / "cloud_key"
        primary = tmp_path / "home" / ".dango" / "known_hosts"
        manager = SSHManager(key_path=key_path, known_hosts_path=primary)
        assert manager.legacy_known_hosts_paths == []
        _pin(key_path.parent / "known_hosts", _HOST)
        assert manager.legacy_known_hosts_paths == [key_path.parent / "known_hosts"]
        same = SSHManager(key_path=key_path, known_hosts_path=key_path.parent / "known_hosts")
        assert same.legacy_known_hosts_paths == []


def _invoke_remote_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    from dango.cli.commands.remote import remote

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = MagicMock()
    cfg.droplet_ip = "1.2.3.4"
    cfg.ssh_key_path = ".dango/cloud_key"
    loader = MagicMock()
    loader.load_cloud_config.return_value = cfg
    key_path = tmp_path / ".dango" / "cloud_key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.touch()

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.config.loader.ConfigLoader", return_value=loader),
        patch("dango.cli.commands.remote_mgmt.os.execvp") as mock_execvp,
    ):
        result = CliRunner().invoke(remote, ["ssh"], obj={"project_root": tmp_path})

    assert result.exit_code == 0, result.output
    return mock_execvp.call_args[0][1], key_path


@pytest.mark.unit
class TestRemoteSshKnownHosts:
    def test_remote_ssh_uses_manager_known_hosts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv, key_path = _invoke_remote_ssh(tmp_path, monkeypatch)
        expected = SSHManager(key_path=key_path).known_hosts_path
        assert expected == tmp_path / "home" / ".dango" / "known_hosts"
        assert f"UserKnownHostsFile={expected}" in argv
        assert "StrictHostKeyChecking=accept-new" in argv

    def test_remote_ssh_carries_over_legacy_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = tmp_path / ".dango" / "known_hosts"
        key = _pin(legacy, "1.2.3.4")
        argv, _ = _invoke_remote_ssh(tmp_path, monkeypatch)
        primary = tmp_path / "home" / ".dango" / "known_hosts"
        assert f"UserKnownHostsFile={primary}" in argv
        # accept-new must not re-TOFU a host already pinned in the legacy file
        assert _pinned_keys(primary, "1.2.3.4") == [key.asbytes()]
