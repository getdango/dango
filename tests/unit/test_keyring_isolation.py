"""tests/unit/test_keyring_isolation.py

The test suite must never read or write the developer's real OS keychain or home directory (K-1).
"""

import os
import subprocess
import sys
from pathlib import Path

import keyring
import pytest
import yaml

from dango.security import MetabaseCredentialStore
from dango.security.token_storage import SecureTokenStorage


def test_tests_use_in_memory_keyring(_isolate_keyring):
    assert keyring.get_keyring() is _isolate_keyring
    assert type(_isolate_keyring).__name__ == "_InMemoryKeyring"


def test_secure_token_storage_does_not_touch_os_keychain(tmp_path, _isolate_keyring):
    storage = SecureTokenStorage(tmp_path)
    key = storage._get_encryption_key()

    stored = _isolate_keyring.get_password("dango-oauth", "master-encryption-key")
    assert stored is not None
    assert stored.encode("utf-8") == key
    assert not (tmp_path / ".dlt" / ".encryption_key").exists()


def test_metabase_credential_store_uses_isolated_keyring(tmp_path, _isolate_keyring):
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "Isolation Test",
                    "id": "keyring-isolation-test",
                    "created_by": "test@example.com",
                    "purpose": "Keyring isolation",
                }
            }
        ),
        encoding="utf-8",
    )
    store = MetabaseCredentialStore(project, cloud_mode=False)
    store.save("s3cret-value")

    assert _isolate_keyring.get_password("dango-metabase", "keyring-isolation-test") == (
        "s3cret-value"
    )
    assert store.secret_path is None
    assert store.load() == "s3cret-value"


def test_fixture_gives_each_test_a_fresh_backend(_isolate_keyring):
    assert _isolate_keyring.get_password("dango-oauth", "master-encryption-key") is None
    assert _isolate_keyring.get_password("dango-metabase", "keyring-isolation-test") is None


def test_subprocess_sees_fail_keyring_backend():
    result = subprocess.run(
        [sys.executable, "-c", "import keyring; print(type(keyring.get_keyring()).__module__)"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=True,
    )
    assert result.stdout.strip() == "keyring.backends.fail"


def test_home_is_isolated(tmp_path_factory):
    home = Path.home()
    assert os.environ["HOME"] == str(home)
    base = tmp_path_factory.getbasetemp().resolve()
    assert base in home.resolve().parents


@pytest.mark.parametrize("override", ["/explicit/home"])
def test_test_can_still_override_home(monkeypatch, override):
    monkeypatch.setenv("HOME", override)
    assert Path.home() == Path(override)
