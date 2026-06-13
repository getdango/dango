"""tests/unit/test_data_fixes.py

Tests for P3-1 (geo seed provisioning), P7-1 (GA4 date cast), P7-3 (staging schema cleanup).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.config.models import DataSource, SourceType
from dango.transformation.generator import DbtModelGenerator


def _make_source(name: str, source_type: str) -> DataSource:
    """Create a minimal DataSource for testing."""
    return DataSource(name=name, type=SourceType(source_type))


# ---------------------------------------------------------------------------
# Fix 1: Geo seed auto-provisioning (P3-1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeoTargetsProvisioning:
    def test_geo_targets_copied_when_missing(self, tmp_path: Path):
        """Google Ads source + no seed file -> seed + model created."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)

        provisioned = gen._provision_geo_targets("my_google_ads")

        # Seed file should be created
        seed_file = project_root / "dbt" / "seeds" / "geo_targets.csv"
        assert seed_file.exists()
        assert "geo_targets.csv" in provisioned

        # Staging model should be created
        model_file = (
            project_root / "dbt" / "models" / "staging" / "stg_my_google_ads__geo_names.sql"
        )
        assert model_file.exists()
        assert "stg_my_google_ads__geo_names.sql" in provisioned

        # Model should reference the correct source name
        content = model_file.read_text()
        assert "my_google_ads" in content
        assert "__SOURCE_NAME__" not in content

    def test_geo_targets_not_copied_when_exists(self, tmp_path: Path):
        """Google Ads source + seed already exists -> no overwrite."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        # Pre-create seed with custom content
        seeds_dir = project_root / "dbt" / "seeds"
        seeds_dir.mkdir(parents=True)
        seed_file = seeds_dir / "geo_targets.csv"
        seed_file.write_text("custom_content")

        # Pre-create model
        staging_dir = project_root / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)
        model_file = staging_dir / "stg_my_ads__geo_names.sql"
        model_file.write_text("custom_model")

        gen = DbtModelGenerator(project_root)
        provisioned = gen._provision_geo_targets("my_ads")

        # Nothing should be provisioned
        assert provisioned == []

        # Original content preserved
        assert seed_file.read_text() == "custom_content"
        assert model_file.read_text() == "custom_model"

    def test_geo_targets_skipped_for_non_google_ads(self, tmp_path: Path):
        """Non-Google-Ads source -> no geo seed provisioning."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_stripe", "stripe")

        # Mock DuckDB to return no tables (so generate_all_models skips gracefully)
        with patch.object(gen, "_discover_tables_from_db", return_value=[]):
            gen.generate_all_models([source])

        # No seed file should exist
        seed_file = project_root / "dbt" / "seeds" / "geo_targets.csv"
        assert not seed_file.exists()

    def test_geo_targets_called_in_generate_all_models(self, tmp_path: Path):
        """generate_all_models calls _provision_geo_targets for google_ads sources."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_ads", "google_ads")

        with (
            patch.object(gen, "_provision_geo_targets") as mock_provision,
            patch.object(gen, "_discover_tables_from_db", return_value=[]),
        ):
            gen.generate_all_models([source])
            mock_provision.assert_called_once_with("my_ads")


# ---------------------------------------------------------------------------
# Fix 2: GA4 date column cast (P7-1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGA4DateCast:
    def test_ga4_date_column_cast(self, tmp_path: Path):
        """GA4 source with TIMESTAMP date column -> SQL contains REPLACE cast."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_ga4", "google_analytics")

        sql = gen.generate_staging_model(
            source=source,
            table_name="sessions",
            schema_name="raw_my_ga4",
            date_cast_columns=["date"],
        )

        assert "REPLACE" in sql
        assert "CAST(date AS DATE) AS date" in sql
        assert "source('my_ga4', 'sessions')" in sql

    def test_non_ga4_no_date_cast(self, tmp_path: Path):
        """Non-GA4 source -> plain SELECT * with no REPLACE."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_stripe", "stripe")

        sql = gen.generate_staging_model(
            source=source,
            table_name="charges",
            schema_name="raw_my_stripe",
        )

        assert "REPLACE" not in sql
        assert "SELECT *" in sql
        assert "source('my_stripe', 'charges')" in sql

    def test_ga4_no_cast_when_already_date(self, tmp_path: Path):
        """GA4 source where date column is already DATE -> no REPLACE clause."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_ga4", "google_analytics")

        # Empty date_cast_columns means the column was already DATE type
        sql = gen.generate_staging_model(
            source=source,
            table_name="sessions",
            schema_name="raw_my_ga4",
            date_cast_columns=[],
        )

        assert "REPLACE" not in sql
        assert "SELECT *" in sql

    def test_ga4_date_detection_in_generate_all_models(self, tmp_path: Path):
        """generate_all_models detects TIMESTAMP date columns for GA4 sources."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_ga4", "google_analytics")

        mock_columns = [
            {
                "name": "date",
                "type": "TIMESTAMP WITH TIME ZONE",
                "nullable": True,
                "tests": [],
                "description": "date column",
            },
            {
                "name": "sessions",
                "type": "BIGINT",
                "nullable": True,
                "tests": [],
                "description": "sessions column",
            },
        ]

        with (
            patch.object(gen, "_discover_tables_from_db", return_value=["sessions"]),
            patch.object(gen, "get_table_schema", return_value=mock_columns),
            patch.object(gen, "generate_staging_model", return_value="-- mock") as mock_gen,
        ):
            gen.generate_all_models([source], generate_schema_yml=False)

            mock_gen.assert_called_once()
            assert mock_gen.call_args.kwargs.get("date_cast_columns") == ["date"]

    def test_ga4_no_detection_when_date_is_date_type(self, tmp_path: Path):
        """generate_all_models does NOT add date_cast_columns when type is already DATE."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / "data").mkdir()

        gen = DbtModelGenerator(project_root)
        source = _make_source("my_ga4", "google_analytics")

        mock_columns = [
            {
                "name": "date",
                "type": "DATE",
                "nullable": True,
                "tests": [],
                "description": "date column",
            },
            {
                "name": "sessions",
                "type": "BIGINT",
                "nullable": True,
                "tests": [],
                "description": "sessions column",
            },
        ]

        with (
            patch.object(gen, "_discover_tables_from_db", return_value=["sessions"]),
            patch.object(gen, "get_table_schema", return_value=mock_columns),
            patch.object(gen, "generate_staging_model", return_value="-- mock") as mock_gen,
        ):
            gen.generate_all_models([source], generate_schema_yml=False)

            mock_gen.assert_called_once()
            assert mock_gen.call_args.kwargs.get("date_cast_columns") == []


# ---------------------------------------------------------------------------
# Fix 3: Staging schema cleanup (P7-3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStagingSchemaCleanup:
    """Test staging schema cleanup in run_sync.

    Uses skip_dbt=True to minimize mocking scope. Model generation and
    post-sync hooks still run but are mocked.
    """

    def test_staging_schemas_dropped_after_success(self):
        """Staging schemas are dropped for successful sources."""
        from dango.ingestion.dlt_runner import run_sync

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("raw_my_source_staging",),
        ]

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_source.return_value = {"status": "success"}

        mock_generator = MagicMock()
        mock_generator.generate_all_models.return_value = {
            "generated": [],
            "skipped": [],
            "errors": [],
        }

        with (
            patch(
                "dango.ingestion.dlt_runner.DltPipelineRunner",
                return_value=mock_runner_instance,
            ),
            patch("duckdb.connect", return_value=mock_conn),
            patch(
                "dango.transformation.generator.DbtModelGenerator",
                return_value=mock_generator,
            ),
            patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value=None),
        ):
            source = _make_source("my_source", "stripe")
            run_sync(Path("/fake/project"), [source], skip_dbt=True)

            # Verify staging schema was dropped
            drop_calls = [
                call for call in mock_conn.execute.call_args_list if "DROP SCHEMA" in str(call)
            ]
            assert len(drop_calls) == 1
            assert "raw_my_source_staging" in str(drop_calls[0])

    def test_staging_schemas_not_dropped_on_failure(self):
        """No staging schemas dropped when all sources fail."""
        from dango.ingestion.dlt_runner import run_sync

        mock_runner_instance = MagicMock()
        mock_runner_instance.run_source.return_value = {
            "status": "error",
            "error": "Connection failed",
        }

        with (
            patch(
                "dango.ingestion.dlt_runner.DltPipelineRunner",
                return_value=mock_runner_instance,
            ),
            patch("duckdb.connect") as mock_connect,
        ):
            source = _make_source("my_source", "stripe")
            run_sync(Path("/fake/project"), [source], skip_dbt=True)

            # duckdb.connect should NOT have been called (cleanup block skipped)
            mock_connect.assert_not_called()
