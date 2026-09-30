"""tests/unit/test_source_setup_service.py

Tests for the non-interactive source setup service (schema, normalisation, prepare/apply/create).
"""

from pathlib import Path
from typing import Any

import pytest

from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.registry import SOURCE_REGISTRY, get_source_metadata
from dango.ingestion.sources.setup_service import (
    REPLACE_MODE_SOURCE_TYPES,
    SourceSetupError,
    compute_env_var_name,
    create_source,
    get_setup_schema,
    normalize_params,
    prepare_source,
    provision_geo_targets,
)


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real Dango project on disk (one CSV source named 'test_source')."""
    save_config(sample_config, tmp_path)
    for var in ("STRIPE_TEST_API_KEY", "STRIPE_ORDERS_API_KEY", "FB_ADS_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _field(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return next(f for f in schema["fields"] if f["name"] == name)


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.unit
class TestSchema:
    def test_schema_local_files_fields_and_defaults(self):
        schema = get_setup_schema("local_files")
        directory = _field(schema, "directory")
        assert directory["default"] == "data/uploads"
        assert directory["managed_by"] == "agent"
        assert schema["supports_empty_sync_policy"] is True
        assert schema["setup_supported"] is True
        assert schema["auth_type"] == "none"

    def test_schema_stripe_secret_field_is_user_secret(self):
        field = _field(get_setup_schema("stripe"), "stripe_secret_key_env")
        assert field["managed_by"] == "user_secret"
        assert field["env_var_template"] == "STRIPE_API_KEY"

    def test_schema_google_sheets_oauth(self):
        schema = get_setup_schema("google_sheets")
        assert schema["auth_type"] == "oauth"
        assert _field(schema, "range_names")["type"] == "sheet_selector"

    def test_schema_facebook_ads_credential_param_is_oauth_managed(self):
        field = _field(get_setup_schema("facebook_ads"), "access_token_env")
        assert field["managed_by"] == "oauth"
        assert field["required"] is False

    def test_schema_unknown_type_raises(self):
        with pytest.raises(SourceSetupError):
            get_setup_schema("nope")

    def test_schema_sql_database_raises(self):
        assert get_source_metadata("sql_database") is None
        with pytest.raises(SourceSetupError):
            get_setup_schema("sql_database")

    def test_schema_rest_api_not_setup_supported(self):
        assert get_setup_schema("rest_api")["setup_supported"] is False

    def test_replace_mode_members_are_registry_keys(self):
        missing = [k for k in REPLACE_MODE_SOURCE_TYPES if get_source_metadata(k) is None]
        assert missing == []
        assert "stripe" in REPLACE_MODE_SOURCE_TYPES


@pytest.mark.unit
class TestNormalize:
    def test_normalize_applies_directory_default_with_source_name(self):
        params, reqs = normalize_params("local_files", "orders", {})
        assert params["directory"] == "data/uploads/orders"
        assert params["file_pattern"] == "*"
        assert reqs == []

    def test_normalize_collects_all_errors(self):
        # github: owner + name required; bogus key unknown; access_token_env secret omitted
        with pytest.raises(SourceSetupError) as exc:
            normalize_params("postgres", "pg", {"bogus": 1, "table_names": 5, "schema": ["x"]})
        assert len(exc.value.errors) == 3
        assert any("Unknown parameter 'bogus'" in e for e in exc.value.errors)

    def test_normalize_missing_required_and_empty_required_string(self):
        with pytest.raises(SourceSetupError) as exc:
            normalize_params("github", "gh", {"owner": "   "})
        joined = " ".join(exc.value.errors)
        assert "Missing required parameter 'owner'" in joined
        assert "Missing required parameter 'name'" in joined

    def test_normalize_rejects_secret_value(self):
        literal = "sk_live_SUPERSECRET123"
        with pytest.raises(SourceSetupError) as exc:
            normalize_params("stripe", "stripe_test", {"stripe_secret_key_env": literal})
        message = str(exc.value)
        assert "STRIPE_TEST_API_KEY" in message and "never accepted" in message
        assert literal not in message

    def test_normalize_accepts_computed_env_var_name(self):
        params, reqs = normalize_params(
            "stripe", "stripe_test", {"stripe_secret_key_env": "STRIPE_TEST_API_KEY"}
        )
        assert params["stripe_secret_key_env"] == "STRIPE_TEST_API_KEY"
        assert [r.name for r in reqs] == ["STRIPE_TEST_API_KEY"]

    def test_normalize_facebook_ads_omits_oauth_credential_param(self):
        params, reqs = normalize_params("facebook_ads", "fb_ads", {"account_id": "123"})
        assert "access_token_env" not in params
        assert reqs == []
        with pytest.raises(SourceSetupError):
            normalize_params("facebook_ads", "fb_ads", {"account_id": "1", "access_token_env": "x"})

    @pytest.mark.parametrize(
        ("source_type", "param", "raw", "expected"),
        [
            ("facebook_ads", "initial_load_past_days", "5", 5),
            ("github", "owner", "  acme ", "acme"),
            ("slack", "selected_channels", "a, b", ["a", "b"]),
            ("slack", "start_date", "2026-01-31", "2026-01-31"),
            ("google_analytics", "start_date", "90daysAgo", "90daysAgo"),
            ("hubspot", "resources", "contacts,deals", ["contacts", "deals"]),
            ("google_sheets", "range_names", "Sheet1", ["Sheet1"]),
        ],
    )
    def test_normalize_coercions(self, source_type, param, raw, expected):
        base = {
            "facebook_ads": {"account_id": "1"},
            "github": {"name": "repo", "owner": "o"},
            "google_analytics": {"property_id": "1"},
            "google_sheets": {"spreadsheet_url_or_id": "abc", "range_names": ["S"]},
            "slack": {},
            "hubspot": {},
        }[source_type]
        params, _ = normalize_params(source_type, "src", {**base, param: raw})
        assert params[param] == expected

    def test_normalize_bad_values(self):
        for source_type, base, param, raw in [
            ("slack", {}, "start_date", "31/01/2026"),
            ("hubspot", {}, "resources", ["not_a_resource"]),
            ("facebook_ads", {"account_id": "1"}, "initial_load_past_days", "abc"),
            ("facebook_ads", {"account_id": "1"}, "initial_load_past_days", True),
        ]:
            with pytest.raises(SourceSetupError):
                normalize_params(source_type, "src", {**base, param: raw})

    def test_normalize_type_helpers(self):
        from dango.ingestion.sources.setup_schema import _coerce

        assert _coerce({"type": "number"}, "2.0") == 2
        assert _coerce({"type": "number"}, "2.5") == 2.5
        assert _coerce({"type": "boolean"}, "Yes") is True
        assert _coerce({"type": "boolean"}, "0") is False
        assert _coerce({"type": "json"}, '{"a":1}') == {"a": 1}
        assert _coerce({"type": "multiselect", "choices": ["a", "b"]}, []) == []
        with pytest.raises(ValueError):
            _coerce({"type": "multiselect", "choices": ["a"]}, ["z"])
        with pytest.raises(ValueError):
            _coerce({"type": "choice", "choices": ["a"]}, "z")
        with pytest.raises(ValueError):
            _coerce({"type": "json"}, "{bad")

    def test_normalize_resources_default_and_validation(self):
        meta = SOURCE_REGISTRY["salesforce"]
        params, _ = normalize_params("salesforce", "sf", {})
        assert params["resources"] == meta["default_resources"]
        params, _ = normalize_params("salesforce", "sf", {"resources": "account, lead"})
        assert params["resources"] == ["account", "lead"]
        with pytest.raises(SourceSetupError):
            normalize_params("salesforce", "sf", {"resources": ["bogus"]})

    def test_env_var_name_matches_wizard_examples(self):
        assert (
            compute_env_var_name({"name": "x_env", "env_var": "STRIPE_API_KEY"}, "stripe_test")
            == "STRIPE_TEST_API_KEY"
        )
        assert (
            compute_env_var_name(
                {"name": "x_env", "env_var": "SLACK_ACCESS_TOKEN"}, "marketing_slack"
            )
            == "MARKETING_SLACK_ACCESS_TOKEN"
        )
        assert compute_env_var_name({"name": "token"}, "my-src") == "TOKEN_MY_SRC"


@pytest.mark.unit
class TestPrepare:
    def test_prepare_rejects_duplicate_and_bad_name(self, project):
        with pytest.raises(SourceSetupError, match="already exists"):
            prepare_source(project, "local_files", "test_source", {})
        with pytest.raises(SourceSetupError):
            prepare_source(project, "local_files", "bad-name", {})
        with pytest.raises(SourceSetupError, match="lowercase"):
            prepare_source(project, "local_files", "Orders", {})

    def test_prepare_drops_env_requirement_when_set_in_dotenv(self, project):
        prepared = prepare_source(project, "stripe", "stripe_test", {})
        assert [r.name for r in prepared.credentials_required] == ["STRIPE_TEST_API_KEY"]
        (project / ".env").write_text("STRIPE_TEST_API_KEY=sk_test_x\n")
        prepared = prepare_source(project, "stripe", "stripe_test", {})
        assert prepared.credentials_required == []
        assert prepared.params["stripe_secret_key_env"] == "STRIPE_TEST_API_KEY"

    def test_prepare_oauth_requirement_when_no_credentials(self, project):
        prepared = prepare_source(
            project,
            "google_sheets",
            "sheet1",
            {"spreadsheet_url_or_id": "abc", "range_names": ["Sheet1"]},
        )
        reqs = prepared.credentials_required
        assert [r.kind for r in reqs] == ["oauth"]
        assert reqs[0].command == "dango oauth google_sheets"

    def test_prepare_facebook_ads_single_oauth_requirement_no_env(self, project):
        prepared = prepare_source(project, "facebook_ads", "fb_ads", {"account_id": "1"})
        assert [r.kind for r in prepared.credentials_required] == ["oauth"]
        assert "access_token_env" not in prepared.params

    def test_prepare_writes_nothing(self, project):
        before = _snapshot(project)
        prepare_source(project, "local_files", "orders", {})
        assert _snapshot(project) == before


@pytest.mark.unit
class TestCreate:
    def test_create_local_files_end_to_end(self, project):
        result = create_source(project, "local_files", "orders", {})
        source = load_config(project).sources.get_source("orders")
        assert source is not None
        dumped = source.model_dump(mode="json")
        assert dumped["name"] == "orders"
        assert dumped["type"] == "local_files"
        assert dumped["enabled"] is True
        assert dumped["description"] == "File Import (CSV, JSON, Parquet) - added via wizard"
        assert dumped["empty_sync_policy"] == "block"
        assert dumped["local_files"]["directory"] == "data/uploads/orders"
        assert dumped["local_files"]["file_pattern"] == "*"
        assert (project / "data" / "uploads" / "orders").is_dir()
        assert ".dango/sources.yml" in result.files_changed
        assert "data/uploads/orders" in result.files_changed
        assert result.validation_errors == []
        assert result.source_config["local_files"]["directory"] == "data/uploads/orders"

    def test_create_writes_default_config_without_overwriting(self, project):
        dlt = project / ".dlt"
        dlt.mkdir()
        (dlt / "config.toml").write_text("[sources.google_analytics]\nlookback_days = 99\n")
        result = create_source(project, "google_analytics", "ga", {"property_id": "123"})
        text = (dlt / "config.toml").read_text()
        assert "lookback_days = 99" in text
        assert "traffic" in text  # default queries were added
        assert ".dlt/config.toml" in result.files_changed
        assert load_config(project).sources.get_source("ga").lookback_days == 7  # type: ignore[union-attr]

    def test_create_google_ads_provisions_geo_targets(self, project):
        result = create_source(project, "google_ads", "ads", {})
        assert (project / "dbt" / "seeds" / "geo_targets.csv").exists()
        assert (project / "dbt" / "models" / "staging" / "stg_ads__geo_names.sql").exists()
        assert "dbt/seeds/geo_targets.csv" in result.files_changed

    def test_provision_geo_targets_templates_exist_and_idempotent(self, tmp_path):
        first = provision_geo_targets(tmp_path, "g1")
        assert len(first) == 2
        assert provision_geo_targets(tmp_path, "g1") == []
        # second source: only its own staging model is new
        assert provision_geo_targets(tmp_path, "g2") == ["dbt/models/staging/stg_g2__geo_names.sql"]

    def test_create_secrets_toml_template_for_template_source(self, project):
        result = create_source(project, "salesforce", "sf", {})
        secrets = (project / ".dlt" / "secrets.toml").read_text()
        assert "[sources.sf.credentials]" in secrets
        assert ".dlt/secrets.toml" in result.files_changed
        assert any(r.kind == "secrets_toml" for r in result.credentials_required)

    def test_create_stripe_writes_env_template_without_value(self, project):
        result = create_source(project, "stripe", "stripe_test", {})
        env_text = (project / ".env").read_text()
        assert "STRIPE_TEST_API_KEY" in env_text
        assert ".env" in result.files_changed
        assert load_config(project).sources.get_source("stripe_test").empty_sync_policy == "block"  # type: ignore[union-attr]

    def test_create_rejects_rest_api_and_hidden_types(self, project):
        for source_type in ("rest_api", "dlt_native", "csv"):
            with pytest.raises(SourceSetupError):
                create_source(project, source_type, "x", {})

    def test_create_nothing_written_on_validation_error(self, project):
        before = _snapshot(project)
        with pytest.raises(SourceSetupError):
            create_source(project, "google_analytics", "ga", {"bogus": 1})
        with pytest.raises(SourceSetupError):
            create_source(project, "stripe", "st", {"stripe_secret_key_env": "sk_live_x"})
        with pytest.raises(SourceSetupError):
            create_source(project, "google_analytics", "ga", {"property_id": "1", "bogus": 1})
        assert _snapshot(project) == before

    def test_empty_sync_policy_rules(self, project):
        create_source(project, "local_files", "a", {}, empty_sync_policy="allow")
        assert load_config(project).sources.get_source("a").empty_sync_policy == "allow"  # type: ignore[union-attr]
        with pytest.raises(SourceSetupError):
            create_source(project, "local_files", "b", {}, empty_sync_policy="maybe")
        with pytest.raises(SourceSetupError, match="replace-mode"):
            create_source(project, "freshdesk", "h", {}, empty_sync_policy="allow")
        assert REPLACE_MODE_SOURCE_TYPES  # sanity: set is non-empty
