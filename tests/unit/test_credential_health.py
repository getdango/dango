"""tests/unit/test_credential_health.py

Tests for dango.ingestion.credential_health module.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from dango.ingestion.credential_health import (
    get_cached_credential_health,
    run_credential_checks,
)


@pytest.fixture
def mock_project_root(tmp_path: Path) -> Path:
    """Create a temporary project root."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".dlt").mkdir()
    return project_root


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset the credential health cache before each test."""
    import dango.ingestion.credential_health as ch

    ch._cache = {}
    yield
    ch._cache = {}


def test_no_sources(mock_project_root: Path) -> None:
    """Test with no sources configured."""
    with patch("dango.config.helpers.get_config") as mock_get_config:
        mock_config = Mock()
        mock_config.sources.sources = []
        mock_get_config.return_value = mock_config

        results = run_credential_checks(mock_project_root)

        assert results == []


def test_missing_oauth_credential(mock_project_root: Path) -> None:
    """Test OAuth source with missing credential."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
        patch("dango.oauth.storage.OAuthStorage") as mock_storage_class,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_google_sheets"
        mock_source.type.value = "google_sheets"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.OAUTH}

        # Setup storage - no credential
        mock_storage = Mock()
        mock_storage.get.return_value = None
        mock_storage_class.return_value = mock_storage

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_google_sheets"
        assert results[0]["status"] == "missing"
        assert "dango oauth" in results[0]["detail"]


def test_valid_oauth_credential(mock_project_root: Path) -> None:
    """Test OAuth source with valid credential."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
        patch("dango.oauth.storage.OAuthStorage") as mock_storage_class,
        patch("dango.oauth.validation.validate_token") as mock_validate,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_google_sheets"
        mock_source.type.value = "google_sheets"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.OAUTH}

        # Setup credential
        mock_cred = Mock()
        mock_cred.account_info = "user@example.com"
        mock_cred.is_expiring_soon.return_value = False

        # Setup storage
        mock_storage = Mock()
        mock_storage.get.return_value = mock_cred
        mock_storage_class.return_value = mock_storage

        # Setup validation
        mock_result = Mock()
        mock_result.valid = True
        mock_validate.return_value = mock_result

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_google_sheets"
        assert results[0]["status"] == "ok"
        assert results[0]["detail"] == "user@example.com"


def test_expired_oauth_credential(mock_project_root: Path) -> None:
    """Test OAuth source with expired credential."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
        patch("dango.oauth.storage.OAuthStorage") as mock_storage_class,
        patch("dango.oauth.validation.validate_token") as mock_validate,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_google_sheets"
        mock_source.type.value = "google_sheets"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.OAUTH}

        # Setup credential
        mock_cred = Mock()
        mock_storage = Mock()
        mock_storage.get.return_value = mock_cred
        mock_storage_class.return_value = mock_storage

        # Setup validation - expired
        mock_result = Mock()
        mock_result.valid = False
        mock_result.message = "Token expired"
        mock_validate.return_value = mock_result

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_google_sheets"
        assert results[0]["status"] == "expired"
        assert results[0]["detail"] == "Token expired"


def test_expiring_soon_oauth_credential(mock_project_root: Path) -> None:
    """Test OAuth source with credential expiring soon."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
        patch("dango.oauth.storage.OAuthStorage") as mock_storage_class,
        patch("dango.oauth.validation.validate_token") as mock_validate,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_google_sheets"
        mock_source.type.value = "google_sheets"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.OAUTH}

        # Setup credential
        mock_cred = Mock()
        mock_cred.account_info = "user@example.com"
        mock_cred.is_expiring_soon.return_value = True
        mock_cred.days_until_expiry.return_value = 3

        # Setup storage
        mock_storage = Mock()
        mock_storage.get.return_value = mock_cred
        mock_storage_class.return_value = mock_storage

        # Setup validation
        mock_result = Mock()
        mock_result.valid = True
        mock_validate.return_value = mock_result

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_google_sheets"
        assert results[0]["status"] == "expiring_soon"
        assert "3 day" in results[0]["detail"]


def test_api_key_present(mock_project_root: Path) -> None:
    """Test API key source with credential present in .env."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # Create .env file
        env_file = mock_project_root / ".env"
        env_file.write_text("STRIPE_API_KEY=sk_test_123456\n")

        # Setup config
        mock_source = Mock()
        mock_source.name = "my_stripe"
        mock_source.type.value = "stripe"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {
            "auth_type": AuthType.API_KEY,
            "required_params": [
                {"name": "stripe_api_key", "type": "secret", "env_var": "STRIPE_API_KEY"}
            ],
            "optional_params": [],
        }

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_stripe"
        assert results[0]["status"] == "ok"


def test_api_key_missing(mock_project_root: Path) -> None:
    """Test API key source with credential missing."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # No .env file created

        # Setup config
        mock_source = Mock()
        mock_source.name = "my_stripe"
        mock_source.type.value = "stripe"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {
            "auth_type": AuthType.API_KEY,
            "required_params": [
                {"name": "stripe_api_key", "type": "secret", "env_var": "STRIPE_API_KEY"}
            ],
            "optional_params": [],
        }

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_stripe"
        assert results[0]["status"] == "missing"
        assert "STRIPE_API_KEY" in results[0]["detail"]


def test_service_account_missing(mock_project_root: Path) -> None:
    """Test service account source with missing secrets.toml."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # No secrets.toml file created

        # Setup config
        mock_source = Mock()
        mock_source.name = "my_gcs"
        mock_source.type.value = "gcs"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.SERVICE_ACCOUNT}

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_gcs"
        assert results[0]["status"] == "missing"
        assert "secrets.toml" in results[0]["detail"]


def test_service_account_present(mock_project_root: Path) -> None:
    """Test service account source with secrets.toml present."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # Create secrets.toml file with source-specific section
        secrets_file = mock_project_root / ".dlt" / "secrets.toml"
        secrets_file.write_text(
            "[sources.gcs]\n"
            'type = "service_account"\n'
            'project_id = "my-project"\n'
            'private_key_id = "key123"\n'
        )

        # Setup config
        mock_source = Mock()
        mock_source.name = "my_gcs"
        mock_source.type.value = "gcs"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.SERVICE_ACCOUNT}

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_gcs"
        assert results[0]["status"] == "ok"


def test_no_auth_source(mock_project_root: Path) -> None:
    """Test source that doesn't require authentication."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_csv"
        mock_source.type.value = "csv"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.NONE}

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_csv"
        assert results[0]["status"] == "ok"


def test_cache_returns_stale_results(mock_project_root: Path) -> None:
    """Test that cache returns results within TTL without re-checking."""
    import dango.ingestion.credential_health as ch

    with patch("dango.ingestion.credential_health.run_credential_checks") as mock_run:
        mock_run.return_value = [
            {"source": "test", "type": "csv", "auth_type": "none", "status": "ok", "detail": ""}
        ]

        # First call
        result1 = get_cached_credential_health(mock_project_root)
        assert mock_run.call_count == 1
        assert len(result1) == 1

        # Second call within TTL
        result2 = get_cached_credential_health(mock_project_root)
        assert mock_run.call_count == 1  # No additional call
        assert result1 == result2

        # Simulate cache expiry by manipulating the timestamp
        import time

        cache_key = str(mock_project_root.resolve())
        ch._cache[cache_key] = (result1, time.monotonic() - 400)  # Older than 300s TTL

        # Third call after TTL
        result3 = get_cached_credential_health(mock_project_root)
        assert mock_run.call_count == 2  # Cache expired, new call made
        assert len(result3) == 1


def test_cache_scoped_by_project_root(tmp_path: Path) -> None:
    """Test that cache entries are separate per project_root."""

    # Create two different project roots
    project1 = tmp_path / "project1"
    project2 = tmp_path / "project2"
    project1.mkdir()
    project2.mkdir()
    (project1 / ".dlt").mkdir()
    (project2 / ".dlt").mkdir()

    with patch("dango.ingestion.credential_health.run_credential_checks") as mock_run:
        # Setup different results for each project
        def side_effect(root: Path) -> list[dict]:
            if root == project1:
                return [
                    {
                        "source": "proj1_source",
                        "type": "csv",
                        "auth_type": "none",
                        "status": "ok",
                        "detail": "",
                    }
                ]
            else:
                return [
                    {
                        "source": "proj2_source",
                        "type": "csv",
                        "auth_type": "none",
                        "status": "ok",
                        "detail": "",
                    }
                ]

        mock_run.side_effect = side_effect

        # Call for project1
        result1 = get_cached_credential_health(project1)
        assert result1[0]["source"] == "proj1_source"
        assert mock_run.call_count == 1

        # Call for project2 - should NOT use project1's cache
        result2 = get_cached_credential_health(project2)
        assert result2[0]["source"] == "proj2_source"
        assert mock_run.call_count == 2  # New call, not cached from project1

        # Call for project1 again - should use cached result
        result3 = get_cached_credential_health(project1)
        assert result3[0]["source"] == "proj1_source"
        assert mock_run.call_count == 2  # No new call


def test_service_account_toml_section_missing(mock_project_root: Path) -> None:
    """Test service account source when secrets.toml exists but lacks source section."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
    ):
        # Create secrets.toml with content but no source-specific section
        secrets_file = mock_project_root / ".dlt" / "secrets.toml"
        secrets_file.write_text("[sources]\n")

        # Setup config
        mock_source = Mock()
        mock_source.name = "my_gcs"
        mock_source.type.value = "gcs"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.SERVICE_ACCOUNT}

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["source"] == "my_gcs"
        assert results[0]["status"] == "missing"
        assert "gcs" in results[0]["detail"]


def test_validation_exception_handling(mock_project_root: Path) -> None:
    """Test that validation exceptions are caught and reported as unknown."""
    with (
        patch("dango.config.helpers.get_config") as mock_get_config,
        patch("dango.ingestion.sources.registry.get_source_metadata") as mock_get_metadata,
        patch("dango.oauth.storage.OAuthStorage") as mock_storage_class,
        patch("dango.oauth.validation.validate_token") as mock_validate,
    ):
        # Setup config
        mock_source = Mock()
        mock_source.name = "my_google_sheets"
        mock_source.type.value = "google_sheets"
        mock_config = Mock()
        mock_config.sources.sources = [mock_source]
        mock_get_config.return_value = mock_config

        # Setup registry
        from dango.ingestion.sources.registry import AuthType

        mock_get_metadata.return_value = {"auth_type": AuthType.OAUTH}

        # Setup credential
        mock_cred = Mock()
        mock_storage = Mock()
        mock_storage.get.return_value = mock_cred
        mock_storage_class.return_value = mock_storage

        # Setup validation to raise exception
        mock_validate.side_effect = RuntimeError("Network error")

        results = run_credential_checks(mock_project_root)

        assert len(results) == 1
        assert results[0]["status"] == "unknown"
        assert "Failed to validate" in results[0]["detail"]
