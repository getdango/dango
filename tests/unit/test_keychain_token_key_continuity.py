"""tests/unit/test_keychain_token_key_continuity.py

The Metabase-password encryption key must stay usable when the OS keychain answers differently between runs (allow, deny, locked, empty), using the real SecureTokenStorage and a scripted keyring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import keyring
import pytest
from cryptography.fernet import Fernet, InvalidToken

from dango.security.token_storage import SecureTokenStorage
from tests.factories.scripted_keyring import ScriptedKeyring

SERVICE = "dango-oauth"
KEY_NAME = "master-encryption-key"
PW1 = "planted-secret-one-7f3a"
PW2 = "planted-secret-two-91bc"


@pytest.fixture
def kr(_isolate_keyring) -> ScriptedKeyring:
    backend = ScriptedKeyring()
    keyring.set_keyring(backend)
    return backend


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return tmp_path / "project"


def _enc(project: Path, password: str) -> str:
    return SecureTokenStorage(project).encrypt_token({"metabase_password": password})


def _dec(project: Path, token: str) -> str:
    return str(SecureTokenStorage(project).decrypt_token(token)["metabase_password"])


def _file_key(project: Path) -> Path:
    return project / ".dlt" / ".encryption_key"


@pytest.mark.unit
class TestKeyContinuityAcrossKeychainAnswers:
    def test_allow_deny_allow_keeps_every_password_readable(self, kr, project):
        enc1 = _enc(project, PW1)  # keychain allowed: key lives in the keychain
        kr.mode = "deny"
        enc2 = _enc(project, PW2)  # user pressed Deny: a file key is used
        kr.mode = "allow"

        assert _dec(project, enc1) == PW1
        assert _dec(project, enc2) == PW2

    def test_deny_then_allow_reuses_the_file_key_and_adds_no_keychain_key(self, kr, project):
        kr.mode = "deny"
        enc1 = _enc(project, PW1)
        key_bytes = _file_key(project).read_bytes()
        kr.mode = "allow"

        assert _dec(project, enc1) == PW1
        assert _enc(project, PW2)  # a later write keeps working
        assert kr.store == {}, "no replacement key may be minted while a file key exists"
        assert _file_key(project).read_bytes() == key_bytes

    @pytest.mark.parametrize("mode", ["deny", "locked", "secauth", "windows"])
    def test_unreadable_keychain_never_creates_a_replacement_key(self, kr, project, mode):
        enc1 = _enc(project, PW1)  # key in the keychain, no file key
        kr.mode = mode

        with pytest.raises(InvalidToken):
            _dec(project, enc1)

        assert not _file_key(project).exists(), "a decrypt must never create a key"
        kr.mode = "allow"
        assert _dec(project, enc1) == PW1  # recovers by itself once the keychain answers

    def test_decrypt_failure_names_the_cause_and_the_recovery_step(self, kr, project, capsys):
        enc1 = _enc(project, PW1)
        capsys.readouterr()
        kr.mode = "locked"

        with pytest.raises(InvalidToken) as excinfo:
            _dec(project, enc1)

        shown = capsys.readouterr().out + str(excinfo.value)
        assert "keychain" in shown.lower()
        assert "dango auth metabase-repair" in shown

    def test_restored_project_on_a_machine_with_an_empty_keychain_fails_honestly(
        self, kr, project, tmp_path, capsys
    ):
        enc1 = _enc(project, PW1)  # "machine 1": key in its keychain
        new_machine = ScriptedKeyring()
        keyring.set_keyring(new_machine)
        restored = tmp_path / "restored"  # auth database came along, the keychain did not
        capsys.readouterr()

        with pytest.raises(InvalidToken) as excinfo:
            _dec(restored, enc1)

        assert new_machine.store == {}, "reading must not mint a key on the new machine"
        assert not _file_key(restored).exists()
        shown = capsys.readouterr().out + str(excinfo.value)
        assert "dango auth metabase-repair" in shown
        # writing a fresh value is the documented recovery and still works
        assert _dec(restored, _enc(restored, PW2)) == PW2

    def test_decrypt_with_wrong_keys_names_a_mismatch_not_a_missing_key(self, kr, project, capsys):
        enc1 = _enc(project, PW1)
        kr.store[(SERVICE, KEY_NAME)] = Fernet.generate_key().decode("utf-8")  # another keychain
        capsys.readouterr()

        with pytest.raises(InvalidToken) as excinfo:
            _dec(project, enc1)

        shown = capsys.readouterr().out + str(excinfo.value)
        assert "does not match" in shown
        assert "dango auth metabase-repair" in shown


@pytest.mark.unit
class TestCharacterisation:
    def test_projects_share_the_machine_key_and_fallback_keys_are_per_project(self, kr, tmp_path):
        project_a, project_b = tmp_path / "a", tmp_path / "b"
        enc_a = _enc(project_a, PW1)
        assert _dec(project_b, enc_a) == PW1  # one machine-wide keychain key

        kr.mode = "deny"
        enc_a2 = _enc(project_a, PW2)
        assert _file_key(project_a).exists()
        assert not _file_key(project_b).exists()
        assert enc_a2

    def test_service_and_username_are_unchanged(self, kr, project):
        _enc(project, PW1)
        assert list(kr.store) == [(SERVICE, KEY_NAME)]

    def test_missing_backend_is_a_permanent_file_key(self, kr, project):
        kr.mode = "missing"
        enc1 = _enc(project, PW1)
        assert _file_key(project).exists()
        assert _dec(project, enc1) == PW1
        assert kr.store == {}


@pytest.mark.unit
class TestUpgradeFromThe1014Layout:
    """States written by 1.0.14 code (the module is byte-identical in 1.0.15) stay readable."""

    def test_file_key_state_is_read_unchanged_under_any_keychain(self, kr, project):
        key = Fernet.generate_key()
        _file_key(project).parent.mkdir(parents=True)
        _file_key(project).write_bytes(key)
        token = Fernet(key).encrypt(json.dumps({"metabase_password": PW1}).encode()).decode()

        for mode in ("missing", "deny", "allow"):
            kr.mode = mode
            assert _dec(project, token) == PW1

        assert _file_key(project).read_bytes() == key
        assert kr.writes() == [], "nothing may be written to the keychain"

    def test_keychain_key_state_is_read_unchanged(self, kr, project):
        key = Fernet.generate_key()
        kr.store[(SERVICE, KEY_NAME)] = key.decode("utf-8")
        token = Fernet(key).encrypt(json.dumps({"metabase_password": PW1}).encode()).decode()

        assert _dec(project, token) == PW1
        assert kr.writes() == []
        assert not _file_key(project).exists()
        assert kr.store[(SERVICE, KEY_NAME)] == key.decode("utf-8")


@pytest.mark.unit
class TestNothingSecretIsShown:
    def test_no_secret_or_key_in_output_logs_or_exception_text(self, kr, project, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        seen: list[str] = []

        def capture_failure(token: str) -> None:
            try:
                _dec(project, token)
            except InvalidToken as exc:
                seen.extend([str(exc), repr(exc)])

        enc1 = _enc(project, PW1)
        kr.mode = "deny"
        _enc(project, PW2)
        kr.mode = "locked"
        capture_failure(enc1)
        kr.mode = "allow"
        kr.store[(SERVICE, KEY_NAME)] = Fernet.generate_key().decode("utf-8")
        capture_failure(enc1)
        keys = {_file_key(project).read_text(encoding="utf-8"), *kr.store.values()}
        new_machine = ScriptedKeyring()
        keyring.set_keyring(new_machine)
        capture_failure(enc1)

        captured = capsys.readouterr()
        shown = captured.out + captured.err + caplog.text + "".join(seen)
        for secret in (PW1, PW2, *keys):
            assert secret and secret not in shown
        assert "dango auth metabase-repair" in shown  # the failing paths did print a message
