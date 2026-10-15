"""tests/unit/test_known_hosts_store.py

Dango's known_hosts store: verbatim preservation, @revoked keys, locked merge of concurrent pins."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest

from dango.exceptions import CloudSSHError
from dango.platform.cloud.ssh import _TOFUHostKeyPolicy

_HOST = "10.0.0.1"


def _key() -> paramiko.PKey:
    return paramiko.ECDSAKey.generate(bits=256)


def _line(name: str, key: paramiko.PKey) -> str:
    return f"{name} {key.get_name()} {key.get_base64()}"


def _pinned(path: Path, name: str) -> list[bytes]:
    entry = paramiko.HostKeys(str(path)).lookup(name)
    return [k.asbytes() for k in entry.values()] if entry else []


@pytest.mark.unit
class TestVerbatimSave:
    def test_new_pin_keeps_every_other_line_in_place(self, tmp_path: Path) -> None:
        kh = tmp_path / "known_hosts"
        original = [
            "# managed by hand",
            "",
            "@cert-authority *.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==",
            f"@revoked * {_key().get_name()} {_key().get_base64()}",
            "198.51.100.8 ssh-unknown-type AAAAB3NzaC1yc2E=",
            "198.51.100.9 ssh-ed25519 not*valid*base64!!",
            _line("198.51.100.7", _key()),
        ]
        kh.write_text("\n".join(original) + "\n")
        new = _key()

        _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), _HOST, new)

        lines = kh.read_text().splitlines()
        assert lines[: len(original)] == original
        assert lines[len(original) :] == [_line(_HOST, new)]
        assert stat.S_IMODE(kh.stat().st_mode) == 0o600

    def test_new_file_created_0600(self, tmp_path: Path) -> None:
        kh = tmp_path / "sub" / "known_hosts"
        _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), _HOST, _key())
        assert stat.S_IMODE(kh.stat().st_mode) == 0o600
        assert (tmp_path / "sub" / "known_hosts.lock").exists()


@pytest.mark.unit
class TestRevokedKeys:
    @pytest.mark.parametrize("where", ["primary", "legacy"])
    def test_revoked_key_never_tofu_accepted(self, tmp_path: Path, where: str) -> None:
        kh = tmp_path / "home" / "known_hosts"
        legacy = tmp_path / "old" / "known_hosts"
        revoked = _key()
        target = kh if where == "primary" else legacy
        target.parent.mkdir(parents=True)
        target.write_text(f"@revoked * {revoked.get_name()} {revoked.get_base64()}\n")
        legacy_paths = [legacy] if where == "legacy" else []

        with pytest.raises(CloudSSHError, match="revoked"):
            _TOFUHostKeyPolicy(kh, legacy_paths).missing_host_key(MagicMock(), _HOST, revoked)
        assert not kh.exists() or _pinned(kh, _HOST) == []

    def test_pinned_but_revoked_key_refused(self, tmp_path: Path) -> None:
        kh = tmp_path / "known_hosts"
        key = _key()
        kh.write_text(
            _line(_HOST, key) + "\n" + f"@revoked 10.* {key.get_name()} {key.get_base64()}\n"
        )
        with pytest.raises(CloudSSHError, match="revoked"):
            _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), _HOST, key)


@pytest.mark.unit
class TestConcurrentPinning:
    def test_two_savers_both_pins_kept(self, tmp_path: Path) -> None:
        kh = tmp_path / "known_hosts"
        a = _TOFUHostKeyPolicy(kh)  # both load the same (empty) state
        b = _TOFUHostKeyPolicy(kh)
        key_a, key_b = _key(), _key()

        a.missing_host_key(MagicMock(), "10.0.0.1", key_a)
        b.missing_host_key(MagicMock(), "10.0.0.2", key_b)

        assert _pinned(kh, "10.0.0.1") == [key_a.asbytes()]
        assert _pinned(kh, "10.0.0.2") == [key_b.asbytes()]
        assert (tmp_path / "known_hosts.lock").exists()

    def test_concurrent_different_key_for_same_host_refused(self, tmp_path: Path) -> None:
        kh = tmp_path / "known_hosts"
        a = _TOFUHostKeyPolicy(kh)
        b = _TOFUHostKeyPolicy(kh)
        first = _key()
        a.missing_host_key(MagicMock(), _HOST, first)
        with pytest.raises(CloudSSHError, match="changed"):
            b.missing_host_key(MagicMock(), _HOST, _key())
        assert _pinned(kh, _HOST) == [first.asbytes()]
