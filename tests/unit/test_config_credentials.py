"""tests/unit/test_config_credentials.py

Tests for dango.config.credentials — CredentialManager.
"""

import pytest
import toml

from dango.config.credentials import CredentialManager, init_dlt_directory


@pytest.mark.unit
class TestCredentialManagerInit:
    def test_path_setup(self, tmp_path):
        cm = CredentialManager(tmp_path)
        assert cm.project_root == tmp_path
        assert cm.dlt_dir == tmp_path / ".dlt"
        assert cm.secrets_file == tmp_path / ".dlt" / "secrets.toml"
        assert cm.config_file == tmp_path / ".dlt" / "config.toml"

    def test_string_path_converted(self, tmp_path):
        cm = CredentialManager(str(tmp_path))
        assert cm.project_root == tmp_path


@pytest.mark.unit
class TestInitDltDirectory:
    def test_creates_dir_and_files(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.init_dlt_directory()
        assert cm.dlt_dir.exists()
        assert cm.secrets_file.exists()
        assert cm.config_file.exists()

    def test_idempotent(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.init_dlt_directory()
        original_secrets = cm.secrets_file.read_text()
        cm.init_dlt_directory()
        assert cm.secrets_file.read_text() == original_secrets

    def test_updates_gitignore(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.init_dlt_directory()
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".dlt/secrets.toml" in gitignore
        assert ".dlt/*.db" in gitignore


@pytest.mark.unit
class TestUpdateGitignore:
    def test_creates_if_missing(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm._update_gitignore()
        assert (tmp_path / ".gitignore").exists()

    def test_appends_to_existing(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.pyc\n")
        cm = CredentialManager(tmp_path)
        cm._update_gitignore()
        content = (tmp_path / ".gitignore").read_text()
        assert "*.pyc" in content
        assert ".dlt/secrets.toml" in content

    def test_idempotent(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm._update_gitignore()
        first = (tmp_path / ".gitignore").read_text()
        cm._update_gitignore()
        second = (tmp_path / ".gitignore").read_text()
        assert first == second


@pytest.mark.unit
class TestLoadSecrets:
    def test_valid_toml(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text('[sources.google]\nclient_id = "abc"\n')
        result = cm.load_secrets()
        assert result["sources"]["google"]["client_id"] == "abc"

    def test_missing_file_returns_empty(self, tmp_path):
        cm = CredentialManager(tmp_path)
        assert cm.load_secrets() == {}

    def test_empty_file(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text("")
        assert cm.load_secrets() == {}


@pytest.mark.unit
class TestLoadConfig:
    def test_valid_toml(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.config_file.write_text("[load]\ntruncate_staging_dataset = true\n")
        result = cm.load_config()
        assert result["load"]["truncate_staging_dataset"] is True

    def test_missing_file_returns_empty(self, tmp_path):
        cm = CredentialManager(tmp_path)
        assert cm.load_config() == {}


@pytest.mark.unit
class TestSaveSecrets:
    def test_creates_file(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets({"sources": {"test": {"key": "val"}}})
        assert cm.secrets_file.exists()
        data = toml.load(cm.secrets_file)
        assert data["sources"]["test"]["key"] == "val"

    def test_merge_mode(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets({"sources": {"a": {"key": "1"}}})
        cm.save_secrets({"sources": {"b": {"key": "2"}}}, merge=True)
        data = toml.load(cm.secrets_file)
        assert data["sources"]["a"]["key"] == "1"
        assert data["sources"]["b"]["key"] == "2"

    def test_overwrite_mode(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets({"sources": {"a": {"key": "1"}}})
        cm.save_secrets({"sources": {"b": {"key": "2"}}}, merge=False)
        data = toml.load(cm.secrets_file)
        assert "a" not in data.get("sources", {})
        assert data["sources"]["b"]["key"] == "2"


@pytest.mark.unit
class TestSaveConfig:
    def test_creates_file(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_config({"load": {"truncate": True}})
        assert cm.config_file.exists()

    def test_merge_mode(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_config({"load": {"truncate": True}})
        cm.save_config({"runtime": {"log_level": "INFO"}}, merge=True)
        data = toml.load(cm.config_file)
        assert data["load"]["truncate"] is True
        assert data["runtime"]["log_level"] == "INFO"


@pytest.mark.unit
class TestDeepMerge:
    def test_flat_merge(self, tmp_path):
        cm = CredentialManager(tmp_path)
        result = cm._deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_nested_merge(self, tmp_path):
        cm = CredentialManager(tmp_path)
        base = {"sources": {"a": {"key": "1"}}}
        update = {"sources": {"b": {"key": "2"}}}
        result = cm._deep_merge(base, update)
        assert result["sources"]["a"]["key"] == "1"
        assert result["sources"]["b"]["key"] == "2"

    def test_scalar_overwrite(self, tmp_path):
        cm = CredentialManager(tmp_path)
        result = cm._deep_merge({"a": 1}, {"a": 2})
        assert result == {"a": 2}


@pytest.mark.unit
class TestGetSourceCredentials:
    def test_found(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text('[sources.google]\nclient_id = "abc"\n')
        result = cm.get_source_credentials("google")
        assert result == {"client_id": "abc"}

    def test_not_found(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text('[sources.google]\nclient_id = "abc"\n')
        assert cm.get_source_credentials("stripe") is None

    def test_no_sources_section(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text("[load]\nkey = true\n")
        assert cm.get_source_credentials("google") is None


@pytest.mark.unit
class TestHasCredentials:
    def test_true(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text('[sources.google]\nclient_id = "abc"\n')
        assert cm.has_credentials("google") is True

    def test_false(self, tmp_path):
        cm = CredentialManager(tmp_path)
        assert cm.has_credentials("google") is False


@pytest.mark.unit
class TestDeleteSourceCredentials:
    def test_existing_source(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets({"sources": {"google": {"key": "val"}}}, merge=False)
        assert cm.delete_source_credentials("google") is True
        assert cm.get_source_credentials("google") is None

    def test_nonexistent_source(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.secrets_file.write_text("")
        assert cm.delete_source_credentials("google") is False

    def test_last_source_removes_section(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets({"sources": {"google": {"key": "val"}}}, merge=False)
        cm.delete_source_credentials("google")
        secrets = cm.load_secrets()
        assert "sources" not in secrets


@pytest.mark.unit
class TestListConfiguredSources:
    def test_with_sources(self, tmp_path):
        cm = CredentialManager(tmp_path)
        cm.dlt_dir.mkdir()
        cm.save_secrets(
            {"sources": {"google": {"key": "1"}, "stripe": {"key": "2"}}},
            merge=False,
        )
        result = cm.list_configured_sources()
        assert set(result) == {"google", "stripe"}

    def test_empty(self, tmp_path):
        cm = CredentialManager(tmp_path)
        assert cm.list_configured_sources() == []


@pytest.mark.unit
class TestModuleLevelInitDltDirectory:
    def test_delegates(self, tmp_path):
        init_dlt_directory(tmp_path)
        assert (tmp_path / ".dlt").exists()
        assert (tmp_path / ".dlt" / "secrets.toml").exists()
