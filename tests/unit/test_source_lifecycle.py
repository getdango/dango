"""tests/unit/test_source_lifecycle.py

Tests for non-interactive source update, enable/disable and removal (setup_lifecycle).
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from dango.config.helpers import load_config, save_config
from dango.config.models import DataSource
from dango.ingestion.sources.setup_lifecycle import (
    remove_source,
    set_source_enabled,
    update_source,
)
from dango.ingestion.sources.setup_schema import SourceSetupError


def _ds(name: str, source_type: str, block: dict[str, Any] | None = None, **extra: Any):
    data: dict[str, Any] = {"name": name, "type": source_type, **extra}
    if block is not None:
        data[source_type] = block
    return DataSource(**data)


def _local(name: str, **kw: Any) -> DataSource:
    return _ds(
        name, "local_files", {"directory": f"data/uploads/{name}", "file_pattern": "*.csv"}, **kw
    )


@pytest.fixture
def project(tmp_path: Path, sample_config) -> Path:
    """Real project on disk with sources: orders, orders_eu (local_files)."""
    sample_config.sources.sources = [_local("orders"), _local("orders_eu")]
    save_config(sample_config, tmp_path)
    return tmp_path


def _add_sources(root: Path, *sources: DataSource) -> None:
    cfg = load_config(root)
    cfg.sources.sources.extend(sources)
    save_config(cfg, root)


def _write(root: Path, rel: str, text: str = "select 1") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _stage(root: Path, name: str, table: str = "t") -> list[str]:
    paths = [
        f"dbt/models/staging/stg_{name}__{table}.sql",
        f"dbt/models/staging/sources_{name}.yml",
        f"dbt/models/staging/stg_{name}.yml",
    ]
    for p in paths:
        _write(root, p)
    return paths


def _source(root: Path, name: str) -> DataSource:
    src = load_config(root).sources.get_source(name)
    assert src is not None
    return src


@pytest.mark.unit
class TestUpdate:
    def test_update_local_files_file_pattern(self, project: Path):
        before = (project / ".dango" / "project.yml").read_bytes()
        result = update_source(project, "orders", {"file_pattern": "*.json"})
        src = _source(project, "orders")
        assert src.local_files.file_pattern == "*.json"
        assert str(src.local_files.directory) == "data/uploads/orders"
        assert result.validation_errors == []
        assert (project / ".dango" / "project.yml").read_bytes() == before

    def test_update_collects_all_errors_and_writes_nothing(self, project: Path):
        before = (project / ".dango" / "sources.yml").read_bytes()
        with pytest.raises(SourceSetupError) as exc:
            update_source(project, "orders", {"bogus": 1, "file_pattern": 5}, empty_sync_policy="x")
        assert len(exc.value.errors) >= 3
        assert (project / ".dango" / "sources.yml").read_bytes() == before

    def test_update_preserves_non_registry_keys(self, project: Path):
        _add_sources(
            project,
            _ds(
                "sheet",
                "google_sheets",
                {
                    "spreadsheet_url_or_id": "abc",
                    "range_names": ["A"],
                    "deduplication": "append_only",
                },
            ),
        )
        update_source(project, "sheet", {"range_names": ["A", "B"]})
        block = _source(project, "sheet").google_sheets
        assert block.range_names == ["A", "B"]
        assert block.deduplication.value == "append_only"

    def test_update_keeps_existing_secret_env_names(self, project: Path):
        _add_sources(
            project,
            _ds(
                "pay",
                "stripe",
                {"stripe_secret_key_env": "CUSTOM_PAY_KEY", "start_date": "2024-01-01"},
            ),
        )
        result = update_source(project, "pay", {"start_date": "2024-02-01"})
        assert _source(project, "pay").stripe.stripe_secret_key_env == "CUSTOM_PAY_KEY"
        assert [r.name for r in result.credentials_required if r.kind == "env_var"] == [
            "CUSTOM_PAY_KEY"
        ]

    def test_update_stripe_with_datetime_start_date_other_field(self, project: Path):
        _add_sources(
            project,
            _ds(
                "pay",
                "stripe",
                {"stripe_secret_key_env": "PAY_API_KEY", "start_date": "2024-01-01T00:00:00"},
            ),
        )
        update_source(project, "pay", {"endpoints": ["Charge"]})
        block = _source(project, "pay").stripe
        assert block.endpoints == ["Charge"]
        assert block.start_date.isoformat() == "2024-01-01T00:00:00"

    def test_update_does_not_inject_defaults(self, project: Path):
        _add_sources(project, _ds("crm", "salesforce", {}))
        update_source(project, "crm", description="CRM data")
        stored = yaml.safe_load((project / ".dango" / "sources.yml").read_text())
        crm = next(s for s in stored["sources"] if s["name"] == "crm")
        assert "resources" not in crm["salesforce"]
        assert crm["description"] == "CRM data"

    def test_update_description_and_empty_sync_policy(self, project: Path):
        update_source(project, "orders", description="New", empty_sync_policy="allow")
        src = _source(project, "orders")
        assert src.description == "New"
        assert src.empty_sync_policy == "allow"
        with pytest.raises(SourceSetupError):
            update_source(project, "orders", empty_sync_policy="maybe")

    def test_update_rejects_rest_api_and_unknown_and_empty(self, project: Path):
        _add_sources(
            project, _ds("api", "rest_api", {"base_url": "https://x.test", "endpoints": []})
        )
        with pytest.raises(SourceSetupError, match="dango source edit"):
            update_source(project, "api", {"x": 1})
        with pytest.raises(SourceSetupError, match="not found"):
            update_source(project, "nope", {"file_pattern": "*"})
        with pytest.raises(SourceSetupError, match="Nothing to update"):
            update_source(project, "orders")

    def test_update_directory_outside_project_rejected_and_inside_created(self, project: Path):
        with pytest.raises(SourceSetupError, match="inside the project"):
            update_source(project, "orders", {"directory": "../elsewhere"})
        update_source(project, "orders", {"directory": "data/new_dir"})
        assert (project / "data" / "new_dir").is_dir()


@pytest.mark.unit
class TestEnable:
    def test_set_source_enabled_changed_and_unchanged(self, project: Path):
        assert set_source_enabled(project, "orders", False) is True
        assert _source(project, "orders").enabled is False
        assert set_source_enabled(project, "orders", False) is False
        assert set_source_enabled(project, "orders", True) is True
        with pytest.raises(SourceSetupError):
            set_source_enabled(project, "nope", True)


@pytest.mark.unit
class TestRemove:
    def test_remove_dry_run_reports_without_writing(self, project: Path):
        files = _stage(project, "orders")
        _write(project, ".env", "ORDERS_KEY=secret-value\n")
        _write(
            project,
            ".dango/monitors.yml",
            "enabled: true\nmonitors:\n- name: orders_rows\n  source_table: raw_orders.t\n"
            "  value_expression: COUNT(*)\n",
        )
        snapshot = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
        result = remove_source(project, "orders", dry_run=True)
        assert result.status == "dry_run"
        assert sorted(result.files_removed) == sorted(files)
        assert result.monitors_removed == ["orders_rows"]
        assert result.env_vars_matching == ["ORDERS_KEY"]
        assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == snapshot

    def test_remove_blocks_on_downstream_without_force(self, project: Path):
        _stage(project, "orders", "orders")
        _write(
            project,
            "dbt/models/marts/fct_orders.sql",
            "select * from {{ ref('stg_orders__orders') }}",
        )
        _write(
            project,
            "dbt/models/intermediate/int_x.sql",
            "select * from {{ source('orders', 'raw') }}",
        )
        with pytest.raises(SourceSetupError, match="marts.fct_orders"):
            remove_source(project, "orders")
        assert load_config(project).sources.get_source("orders") is not None
        preview = remove_source(project, "orders", dry_run=True)
        assert preview.downstream_models == ["intermediate.int_x", "marts.fct_orders"]
        result = remove_source(project, "orders", force=True)
        assert result.status == "removed"
        assert load_config(project).sources.get_source("orders") is None

    def test_remove_force_removes_staging_files_only_for_that_source(self, project: Path):
        _stage(project, "orders")
        eu_files = _stage(project, "orders_eu")
        result = remove_source(project, "orders", force=True)
        assert len(result.files_removed) == 3
        assert all((project / f).exists() for f in eu_files)
        assert load_config(project).sources.get_source("orders_eu") is not None

    def test_remove_double_underscore_names_do_not_collide(self, project: Path):
        _add_sources(project, _local("orders__eu"))
        _stage(project, "orders", "a")
        eu_files = _stage(project, "orders__eu", "b")
        _write(
            project, "dbt/models/marts/fct_eu.sql", "select * from {{ ref('stg_orders__eu__b') }}"
        )
        _write(
            project,
            ".dango/monitors.yml",
            "monitors:\n- name: eu_rows\n  source_table: staging.stg_orders__eu__b\n  value_expression: COUNT(*)\n",
        )
        result = remove_source(project, "orders")
        assert result.downstream_models == []
        assert result.monitors_removed == []
        assert all((project / f).exists() for f in eu_files)

    def test_remove_keeps_shared_config_toml_section(self, project: Path):
        ga = {"property_id": "1", "queries": []}
        _add_sources(
            project,
            _ds("ga_a", "google_analytics", {"property_id": "1"}),
            _ds("ga_b", "google_analytics", {"property_id": "2"}),
        )
        _write(
            project,
            ".dlt/config.toml",
            '[sources.google_analytics]\nproperty_id = "1"\n',
        )
        del ga
        first = remove_source(project, "ga_a")
        assert first.config_toml_section_removed is False
        assert any("Kept [sources.google_analytics]" in w and "ga_b" in w for w in first.warnings)
        assert "[sources.google_analytics]" in (project / ".dlt" / "config.toml").read_text()
        second = remove_source(project, "ga_b")
        assert second.config_toml_section_removed is True
        assert "google_analytics" not in (project / ".dlt" / "config.toml").read_text()

    def test_remove_reports_env_var_names_not_values(self, project: Path):
        _write(project, ".env", "ORDERS_API_KEY=supersecret\nOTHER=1\n")
        result = remove_source(project, "orders")
        assert result.env_vars_matching == ["ORDERS_API_KEY"]
        assert "supersecret" not in repr(result)
        assert (project / ".env").read_text() == "ORDERS_API_KEY=supersecret\nOTHER=1\n"
        assert any("dango db clean" in w for w in result.warnings)

    def test_remove_env_vars_exclude_longer_named_source(self, project: Path):
        _add_sources(
            project,
            _ds("stripe", "stripe", {"stripe_secret_key_env": "STRIPE_API_KEY"}),
            _ds("stripe_orders", "stripe", {"stripe_secret_key_env": "STRIPE_ORDERS_API_KEY"}),
        )
        _write(project, ".env", "STRIPE_API_KEY=a\nSTRIPE_ORDERS_API_KEY=b\n")
        assert remove_source(project, "stripe", dry_run=True).env_vars_matching == [
            "STRIPE_API_KEY"
        ]
        assert remove_source(project, "stripe_orders", dry_run=True).env_vars_matching == [
            "STRIPE_ORDERS_API_KEY"
        ]

    def test_remove_deletes_only_this_sources_monitors(self, project: Path):
        _write(
            project,
            ".dango/monitors.yml",
            "enabled: true\nmonitors:\n"
            "- name: o1\n  source_table: raw_orders.charge\n  value_expression: COUNT(*)\n"
            "- name: o2\n  source_table: staging.stg_orders__charge\n  value_expression: COUNT(*)\n"
            "- name: eu\n  source_table: raw_orders_eu.charge\n  value_expression: COUNT(*)\n",
        )
        result = remove_source(project, "orders")
        assert sorted(result.monitors_removed) == ["o1", "o2"]
        kept = yaml.safe_load((project / ".dango" / "monitors.yml").read_text())
        assert [m["name"] for m in kept["monitors"]] == ["eu"]

    def test_remove_unknown_source_lists_available(self, project: Path):
        with pytest.raises(SourceSetupError, match="orders_eu"):
            remove_source(project, "nope")


@pytest.mark.unit
class TestReviewFollowUps:
    def test_update_description_with_stale_stored_date_value(self, project: Path):
        from dango.ingestion.sources.setup_service import create_source

        create_source(project, "kinesis", "events", {"stream_name": "s"})
        update_source(project, "events", description="d")
        assert _source(project, "events").description == "d"

    def test_lifecycle_writes_leave_project_yml_untouched(self, project: Path):
        path = project / ".dango" / "project.yml"
        path.write_text("# my comment\n" + path.read_text() + "foo: bar\n")
        before = path.read_bytes()
        set_source_enabled(project, "orders", False)
        update_source(project, "orders", description="x")
        remove_source(project, "orders_eu")
        assert path.read_bytes() == before

    def test_update_empty_params_is_nothing_to_update(self, project: Path):
        with pytest.raises(SourceSetupError, match="Nothing to update"):
            update_source(project, "orders", {})

    def test_remove_dry_run_lists_downstream_without_raising(self, project: Path):
        _stage(project, "orders", "t")
        _write(project, "dbt/models/marts/fct_o.sql", "select * from {{ ref('stg_orders__t') }}")
        result = remove_source(project, "orders", dry_run=True)
        assert result.downstream_models == ["marts.fct_o"]

    def test_source_remove_cli_flow(self, project: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        from dango.cli.main import cli

        _stage(project, "orders", "t")
        _write(project, "dbt/models/marts/fct_o.sql", "select * from {{ ref('stg_orders__t') }}")
        _write(project, ".env", "ORDERS_KEY=v\nORDERS_EU_KEY=w\n")
        monkeypatch.chdir(project)
        monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: None)
        runner = CliRunner()
        declined = runner.invoke(cli, ["source", "remove", "orders"], input="n\n")
        assert "marts.fct_o" in declined.output and "will break" in declined.output
        assert _source(project, "orders") is not None
        done = runner.invoke(cli, ["source", "remove", "orders"], input="y\nn\n")
        assert done.exit_code == 0
        assert "ORDERS_KEY" in done.output and "ORDERS_EU_KEY" not in done.output
        assert (project / ".env").read_text() == "ORDERS_KEY=v\nORDERS_EU_KEY=w\n"
        missing = runner.invoke(cli, ["source", "remove", "orders"], input="y\n")
        assert missing.exit_code != 0


@pytest.mark.unit
def test_credential_requirements_stripe_stored_env_name(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.ingestion.sources.setup_lifecycle import credential_requirements

    monkeypatch.delenv("MY_STRIPE_KEY", raising=False)
    src = _ds("billing", "stripe", {"stripe_secret_key_env": "MY_STRIPE_KEY"})
    sample_config.sources.sources = [src]
    save_config(sample_config, tmp_path)

    reqs = credential_requirements(tmp_path, src)
    assert [(r.kind, r.name) for r in reqs] == [("env_var", "MY_STRIPE_KEY")]
    (tmp_path / ".env").write_text("MY_STRIPE_KEY=whatever\n")
    assert credential_requirements(tmp_path, src) == []
    assert not (tmp_path / ".dlt" / "secrets.toml").exists()
