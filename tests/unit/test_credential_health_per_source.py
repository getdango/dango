"""tests/unit/test_credential_health_per_source.py

Credential health checks each source's own env var (not the registry's generic name), creates
no files as a side effect, and supports bypassing the in-process cache.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dango.ingestion.credential_health as ch
from dango.config.helpers import save_config
from dango.ingestion.credential_health import (
    get_cached_credential_health,
    run_credential_checks,
)
from dango.ingestion.sources.setup_service import create_source


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BILLING_API_KEY", "STRIPE_API_KEY", "STRIPE_SECRET_KEY_ENV"):
        monkeypatch.delenv(var, raising=False)
    ch._cache = {}
    yield
    ch._cache = {}


@pytest.fixture
def project(tmp_path: Path, sample_config) -> Path:
    save_config(sample_config, tmp_path)
    create_source(tmp_path, "stripe", "billing", {})
    return tmp_path


def _billing(root: Path) -> dict:
    return next(r for r in run_credential_checks(root) if r["source"] == "billing")


@pytest.mark.unit
def test_stripe_source_ok_when_its_own_var_set(project: Path) -> None:
    (project / ".env").write_text("BILLING_API_KEY=dummy\n")
    assert _billing(project)["status"] == "ok"


@pytest.mark.unit
def test_stripe_source_missing_when_only_registry_var_set(project: Path) -> None:
    (project / ".env").write_text("STRIPE_API_KEY=dummy\n")
    result = _billing(project)
    assert result["status"] == "missing"
    assert "BILLING_API_KEY" in result["detail"]
    assert "STRIPE_API_KEY" not in result["detail"]


@pytest.mark.unit
def test_fallback_to_computed_name_when_not_stored(tmp_path: Path) -> None:
    """A source with nothing stored for the secret param falls back to compute_env_var_name."""
    from dango.ingestion.sources.registry import AuthType

    source = Mock()
    source.name = "billing"
    source.type.value = "someapi"  # not a DataSource field -> generic_config (empty)
    source.generic_config = {}
    config = Mock()
    config.sources.sources = [source]
    metadata = {
        "auth_type": AuthType.API_KEY,
        "required_params": [{"name": "api_key", "type": "secret", "env_var": "STRIPE_API_KEY"}],
        "optional_params": [],
    }
    with (
        patch("dango.config.helpers.get_config", return_value=config),
        patch("dango.ingestion.sources.registry.get_source_metadata", return_value=metadata),
    ):
        result = run_credential_checks(tmp_path)[0]
        assert result["status"] == "missing"
        assert "BILLING_API_KEY" in result["detail"]
        (tmp_path / ".env").write_text("BILLING_API_KEY=dummy\n")
        assert run_credential_checks(tmp_path)[0]["status"] == "ok"


@pytest.mark.unit
def test_no_secrets_toml_created_by_check(tmp_path: Path, sample_config) -> None:
    save_config(sample_config, tmp_path)
    create_source(
        tmp_path,
        "google_sheets",
        "sheets",
        {"spreadsheet_url_or_id": "abc123", "range_names": "Sheet1"},
    )
    assert not (tmp_path / ".dlt" / "secrets.toml").exists()
    results = run_credential_checks(tmp_path)
    sheets = next(r for r in results if r["source"] == "sheets")
    assert sheets["status"] == "missing"
    assert "dango oauth google_sheets" in sheets["detail"]
    assert not (tmp_path / ".dlt" / "secrets.toml").exists()


@pytest.mark.unit
def test_refresh_bypasses_cache(project: Path) -> None:
    first = get_cached_credential_health(project)
    assert next(r for r in first if r["source"] == "billing")["status"] == "missing"
    (project / ".env").write_text("BILLING_API_KEY=dummy\n")
    stale = get_cached_credential_health(project)
    assert next(r for r in stale if r["source"] == "billing")["status"] == "missing"
    with patch.object(ch, "run_credential_checks", wraps=ch.run_credential_checks) as spy:
        fresh = get_cached_credential_health(project, refresh=True)
        assert spy.call_count == 1
    assert next(r for r in fresh if r["source"] == "billing")["status"] == "ok"
    # refresh repopulated the cache
    again = get_cached_credential_health(project)
    assert next(r for r in again if r["source"] == "billing")["status"] == "ok"
