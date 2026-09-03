"""tests/unit/test_mcp_mutations.py

Tests for the MCP mutation tools (dango/cli/commands/mcp_mutations.py):
run_sync, run_transform, run_doctor, add_source, list_source_types,
create_model, add_schedule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands import mcp_mutations


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real Dango project on disk (one CSV source named 'test_source'),
    with mcp_mutations._get_project_root() pointed at it — mirrors the
    monkeypatch pattern already used for mcp_server._get_project_root in
    test_mcp_server.py.
    """
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_mutations, "_get_project_root", lambda: tmp_path)
    return tmp_path


@pytest.mark.unit
class TestRunSync:
    def test_run_sync_missing_source(self, project: Path) -> None:
        result = mcp_mutations.run_sync("nonexistent")
        assert result == {"error": "Source 'nonexistent' not found in sources.yml"}

    def test_run_sync_calls_real_function_with_source_object_list(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the source_names-kwarg bug: run_sync()'s real signature
        takes sources: list[DataSource], not a source_names kwarg. Assert the tool calls
        it with a list containing the actual DataSource object, not source name strings."""
        captured = {}

        def fake_run_sync(*, project_root, sources, full_refresh):
            captured["project_root"] = project_root
            captured["sources"] = sources
            captured["full_refresh"] = full_refresh
            return {"status": "completed", "rows_loaded": 42}

        monkeypatch.setattr("dango.ingestion.run_sync", fake_run_sync)

        result = mcp_mutations.run_sync("test_source", full_refresh=True)

        assert result == {"status": "completed", "rows_loaded": 42}
        assert captured["project_root"] == project
        assert len(captured["sources"]) == 1
        assert captured["sources"][0].name == "test_source"
        assert captured["full_refresh"] is True

    def test_run_sync_wraps_exception(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising_run_sync(**kwargs):
            raise RuntimeError("lock held by another process")

        monkeypatch.setattr("dango.ingestion.run_sync", raising_run_sync)

        result = mcp_mutations.run_sync("test_source")
        assert result == {"status": "failed", "error": "lock held by another process"}


@pytest.mark.unit
class TestRunTransform:
    def test_run_transform_success(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (True, "1 of 1 OK"),
        )
        result = mcp_mutations.run_transform()
        assert result == {"status": "completed", "output": "1 of 1 OK"}

    def test_run_transform_failure_is_not_reported_as_completed(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the tuple-truthiness bug: run_dbt_models() returns
        tuple[bool, str] — `if result` on the raw tuple is always truthy regardless of
        the bool inside it, so a real dbt failure must still surface as status='failed'."""
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (False, "Compilation Error in model foo"),
        )
        result = mcp_mutations.run_transform()
        assert result == {"status": "failed", "output": "Compilation Error in model foo"}


@pytest.mark.unit
class TestRunDoctor:
    def test_run_doctor_delegates_to_get_cached_credential_health(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the run_doctor_cached-doesn't-exist bug: the real
        function is get_cached_credential_health() in dango.ingestion.credential_health."""
        expected = [{"source": "test_source", "type": "csv", "status": "ok", "detail": ""}]
        monkeypatch.setattr(
            "dango.ingestion.credential_health.get_cached_credential_health",
            lambda project_root: expected,
        )
        assert mcp_mutations.run_doctor() == expected


@pytest.mark.unit
class TestAddSource:
    def test_add_source_unknown_type(self, project: Path) -> None:
        result = mcp_mutations.add_source("not_a_real_source_type", "my_source")
        assert "error" in result
        assert "Unknown source type" in result["error"]

    def test_add_source_duplicate_name(self, project: Path) -> None:
        result = mcp_mutations.add_source("csv", "test_source")
        assert result == {"error": "Source 'test_source' already exists in sources.yml"}

    def test_add_source_success_writes_sources_yml_and_returns_next_steps(
        self, project: Path
    ) -> None:
        """Positive control for the save_config-argument-order bug: save_config's real
        signature is save_config(config, project_root=None). Reversed arguments would
        raise or silently fail to persist — assert the new source actually round-trips
        through disk, not just that the in-memory return dict looks right."""
        result = mcp_mutations.add_source("stripe", "my_stripe", description="Prod Stripe")

        assert result["status"] == "created"
        assert result["source_name"] == "my_stripe"
        assert result["auth_type"] == "api_key"
        assert any(
            "STRIPE_API_KEY" in step or "secrets.toml" in step for step in result["next_steps"]
        )
        assert any("dango sync my_stripe" in step for step in result["next_steps"])

        from dango.config.helpers import load_config

        reloaded = load_config(project)
        added = reloaded.sources.get_source("my_stripe")
        assert added is not None
        assert added.type.value == "stripe"
        assert added.description == "Prod Stripe"


@pytest.mark.unit
class TestListSourceTypes:
    def test_list_source_types_includes_known_wizard_enabled_type(self) -> None:
        result = mcp_mutations.list_source_types()
        types = {r["type"] for r in result}
        assert "stripe" in types
        stripe_entry = next(r for r in result if r["type"] == "stripe")
        assert stripe_entry["auth_type"] == "api_key"


@pytest.mark.unit
class TestCreateModel:
    def test_create_model_wrong_layer_naming(self, project: Path) -> None:
        result = mcp_mutations.create_model("raw_orders", "marts", [])
        assert "error" in result
        assert "fct_" in result["error"] or "dim_" in result["error"]

    def test_create_model_invalid_layer(self, project: Path) -> None:
        result = mcp_mutations.create_model("stg_x__y", "not_a_layer", [])
        assert "error" in result

    def test_create_model_raw_ref_in_marts(self, project: Path) -> None:
        result = mcp_mutations.create_model("fct_orders", "marts", ["raw_stripe__orders"])
        assert result["status"] == "created"
        assert result["warnings"]
        assert "raw table" in result["warnings"][0]

    def test_create_model_staging_scaffold(self, project: Path) -> None:
        """Also a positive control for the staging source() brace bug found during
        implementation: the scaffold must contain valid dbt Jinja ({{ source(...) }},
        double braces), not the single-brace ({ source(...) }) literal text a plain
        f"{{ source(...) }}" collapses to."""
        result = mcp_mutations.create_model("stg_stripe__orders", "staging", ["orders"])

        assert result["status"] == "created"
        assert result["file_path"] == str(
            Path("dbt") / "models" / "staging" / "stg_stripe__orders.sql"
        )

        model_file = project / "dbt" / "models" / "staging" / "stg_stripe__orders.sql"
        assert model_file.exists()
        content = model_file.read_text()
        assert "{{ source('orders', 'orders') }}" in content
        assert content == result["sql_scaffold"]

        schema_file = project / "dbt" / "models" / "staging" / "schema.yml"
        assert schema_file.exists()

    def test_create_model_rejects_path_traversal(self, project: Path) -> None:
        """Regression-risk item from the session prompt: model_dir / f"{model_name}.sql"
        must stay inside the project — model_name must not escape via '../'."""
        result = mcp_mutations.create_model("stg_x/../../evil", "staging", [])
        assert "error" in result
        escaped_path = project.parent / "evil.sql"
        assert not escaped_path.exists()

    def test_create_model_duplicate_name(self, project: Path) -> None:
        first = mcp_mutations.create_model("int_orders_enriched", "intermediate", [])
        assert first["status"] == "created"
        second = mcp_mutations.create_model("int_orders_enriched", "intermediate", [])
        assert "error" in second
        assert "already exists" in second["error"]


@pytest.mark.unit
class TestAddSchedule:
    def test_add_schedule_missing_source(self, project: Path) -> None:
        result = mcp_mutations.add_schedule("daily_sync", "0 7 * * *", ["nonexistent_source"])
        assert "error" in result
        assert "nonexistent_source" in result["error"]

    def test_add_schedule_success_persists_to_disk(self, project: Path) -> None:
        result = mcp_mutations.add_schedule(
            "daily_sync", "0 7 * * *", ["test_source"], timezone="Asia/Singapore"
        )

        assert result["status"] == "created"
        assert result["schedule_name"] == "daily_sync"

        from dango.config.schedules import load_schedules_config

        reloaded = load_schedules_config(project)
        names = {s.name for s in reloaded.schedules}
        assert "daily_sync" in names

    def test_add_schedule_duplicate_name(self, project: Path) -> None:
        mcp_mutations.add_schedule("daily_sync", "0 7 * * *", ["test_source"])
        result = mcp_mutations.add_schedule("daily_sync", "0 8 * * *", ["test_source"])
        assert result == {"error": "Schedule 'daily_sync' already exists"}
