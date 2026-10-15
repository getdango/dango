"""tests/unit/test_ssh_tofu_policy.py

SSHManager's TOFU policy: pinned hosts never accept another key or key type; legacy pins honoured."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from dango.exceptions import CloudSSHError
from dango.platform.cloud.ssh import _TOFUHostKeyPolicy, ensure_host_pinned

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


def _messy_known_hosts(path: Path, key: paramiko.PKey, *, hashed: bool = False) -> None:
    """A known_hosts with a valid pin for _HOST between lines paramiko cannot load."""
    name = paramiko.HostKeys.hash_host(_HOST) if hashed else _HOST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# comment\n"
        "198.51.100.9 ssh-ed25519 not*valid*base64!!\n"
        "@cert-authority *.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n"
        "198.51.100.8 ssh-unknown-type AAAAB3NzaC1yc2E=\n"
        f"{name} {key.get_name()} {key.get_base64()}\n"
    )


@pytest.mark.unit
class TestTolerantKnownHostsLoading:
    @pytest.mark.parametrize("hashed", [False, True])
    def test_bad_lines_skipped_valid_pin_used(
        self, tmp_path: Path, p256: paramiko.PKey, hashed: bool
    ) -> None:
        kh = tmp_path / "known_hosts"
        _messy_known_hosts(kh, p256, hashed=hashed)
        with patch("dango.platform.cloud.ssh._logger", create=True) as log:
            policy = _TOFUHostKeyPolicy(kh)
        policy.missing_host_key(MagicMock(), _HOST, p256)  # valid pin accepted
        with pytest.raises(CloudSSHError, match="changed"):
            policy.missing_host_key(MagicMock(), _HOST, paramiko.ECDSAKey.generate(bits=256))
        assert log.warning.call_count == 1
        text = repr(log.warning.call_args)
        assert str(kh) in text
        for lineno in ("2", "3", "4"):
            assert lineno in text
        assert "AAAA" not in text and "not*valid" not in text  # no key material

    def test_legacy_file_with_bad_lines_honoured_and_not_rewritten(
        self, tmp_path: Path, p256: paramiko.PKey
    ) -> None:
        kh = tmp_path / "home" / "known_hosts"
        legacy = tmp_path / "user-ssh" / "known_hosts"
        _messy_known_hosts(legacy, p256, hashed=True)
        before = legacy.read_bytes()
        with patch("dango.platform.cloud.ssh._logger", create=True):
            assert ensure_host_pinned(kh, [legacy], _HOST) is True
            with pytest.raises(CloudSSHError, match="changed"):
                _TOFUHostKeyPolicy(kh, legacy_paths=[legacy]).missing_host_key(
                    MagicMock(), _HOST, paramiko.ECDSAKey.generate(bits=384)
                )
        assert legacy.read_bytes() == before
        assert _keys(kh, _HOST) == {p256.get_name(): p256.asbytes()}
