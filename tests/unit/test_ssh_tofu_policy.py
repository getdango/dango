"""tests/unit/test_ssh_tofu_policy.py

SSHManager's TOFU policy: pinned hosts never accept another key or key type; legacy pins honoured."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest

from dango.exceptions import CloudSSHError
from dango.platform.cloud.ssh import _TOFUHostKeyPolicy

_HOST = "10.0.0.1"


def _pin(path: Path, name: str, key: paramiko.PKey) -> None:
    hk = paramiko.HostKeys()
    if path.exists():
        hk.load(str(path))
    hk.add(name, key.get_name(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    hk.save(str(path))


def _keys(path: Path, name: str) -> dict[str, bytes]:
    if not path.exists():
        return {}
    entry = paramiko.HostKeys(str(path)).lookup(name)
    return {t: k.asbytes() for t, k in entry.items()} if entry else {}


@pytest.fixture
def p256() -> paramiko.PKey:
    return paramiko.ECDSAKey.generate(bits=256)


@pytest.mark.unit
class TestTofuPolicy:
    def test_unknown_host_is_pinned(self, tmp_path: Path, p256: paramiko.PKey) -> None:
        kh = tmp_path / "known_hosts"
        _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), _HOST, p256)
        assert _keys(kh, _HOST) == {p256.get_name(): p256.asbytes()}

    def test_other_key_type_for_pinned_host_refused(
        self, tmp_path: Path, p256: paramiko.PKey
    ) -> None:
        kh = tmp_path / "known_hosts"
        _pin(kh, _HOST, p256)
        offered = paramiko.ECDSAKey.generate(bits=384)  # different key type
        assert offered.get_name() != p256.get_name()
        before = kh.read_bytes()

        with pytest.raises(CloudSSHError, match="changed"):
            _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), _HOST, offered)
        assert kh.read_bytes() == before

    def test_other_key_type_refused_on_non_22_port(
        self, tmp_path: Path, p256: paramiko.PKey
    ) -> None:
        kh = tmp_path / "known_hosts"
        _pin(kh, f"[{_HOST}]:2222", p256)
        offered = paramiko.ECDSAKey.generate(bits=384)
        with pytest.raises(CloudSSHError, match="changed"):
            _TOFUHostKeyPolicy(kh).missing_host_key(MagicMock(), f"[{_HOST}]:2222", offered)

    def test_legacy_pin_honoured_and_carried_over(
        self, tmp_path: Path, p256: paramiko.PKey
    ) -> None:
        kh = tmp_path / "home" / "known_hosts"
        legacy = tmp_path / "project" / "known_hosts"
        _pin(legacy, _HOST, p256)

        _TOFUHostKeyPolicy(kh, legacy_paths=[legacy]).missing_host_key(MagicMock(), _HOST, p256)
        assert _keys(kh, _HOST) == {p256.get_name(): p256.asbytes()}

    def test_legacy_pin_blocks_other_key(self, tmp_path: Path, p256: paramiko.PKey) -> None:
        kh = tmp_path / "home" / "known_hosts"
        legacy = tmp_path / "project" / "known_hosts"
        _pin(legacy, _HOST, p256)
        for offered in (paramiko.ECDSAKey.generate(bits=256), paramiko.ECDSAKey.generate(bits=384)):
            with pytest.raises(CloudSSHError, match="changed"):
                _TOFUHostKeyPolicy(kh, legacy_paths=[legacy]).missing_host_key(
                    MagicMock(), _HOST, offered
                )
        assert _keys(kh, _HOST) == {}

    def test_conflicting_primary_and_legacy_refused(
        self, tmp_path: Path, p256: paramiko.PKey
    ) -> None:
        kh = tmp_path / "home" / "known_hosts"
        legacy = tmp_path / "project" / "known_hosts"
        _pin(kh, _HOST, p256)
        _pin(legacy, _HOST, paramiko.ECDSAKey.generate(bits=256))
        with pytest.raises(CloudSSHError, match="changed"):
            _TOFUHostKeyPolicy(kh, legacy_paths=[legacy]).missing_host_key(MagicMock(), _HOST, p256)
