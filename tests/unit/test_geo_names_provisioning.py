"""tests/unit/test_geo_names_provisioning.py

Adding a Google Ads source must not leave a dbt model that references a table nobody has loaded
yet: the geo_names join model appears only once raw_<name>.geographic_stats exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from dango.config.helpers import save_config
from dango.config.models import DataSource, SourceType
from dango.ingestion.sources.setup_service import apply_source, prepare_source
from dango.transformation.generator import DbtModelGenerator

pytestmark = pytest.mark.unit

STAGING = Path("dbt") / "models" / "staging"


@pytest.fixture
def project(tmp_path: Path, sample_config) -> Path:
    """A real Dango project on disk with one CSV source."""
    save_config(sample_config, tmp_path)
    return tmp_path


def _model(root: Path, name: str = "ads") -> Path:
    return root / STAGING / f"stg_{name}__geo_names.sql"


def _warehouse(root: Path, tables: list[str], schema: str = "raw_ads") -> None:
    db = root / "data" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for table in tables:
            conn.execute(
                f"CREATE TABLE {schema}.{table} "
                "(geographic_view_country_criterion_id BIGINT, clicks BIGINT)"
            )
            conn.execute(f"INSERT INTO {schema}.{table} VALUES (2004, 3)")
    finally:
        conn.close()


def _ads() -> DataSource:
    return DataSource(name="ads", type=SourceType.GOOGLE_ADS)


class TestSetupDoesNotWriteTheJoinModel:
    def test_add_google_ads_writes_the_seed_but_not_the_model(self, project: Path) -> None:
        result = apply_source(project, prepare_source(project, "google_ads", "ads", {}))

        assert (project / "dbt" / "seeds" / "geo_targets.csv").exists()
        assert not _model(project).exists()
        assert not any("geo_names" in path for path in result.files_changed)
        assert not list((project / STAGING).glob("*geo_names*"))


class TestGeneratorProvisionsTheModelOnlyWithTheTable:
    def test_no_geographic_stats_table_means_no_model(self, project: Path) -> None:
        _warehouse(project, ["campaigns"])

        summary = DbtModelGenerator(project).generate_all_models([_ads()])

        assert summary["errors"] == []
        assert (project / "dbt" / "seeds" / "geo_targets.csv").exists()
        assert not _model(project).exists()

    def test_no_warehouse_at_all_means_no_model(self, project: Path) -> None:
        DbtModelGenerator(project).generate_all_models([_ads()])

        assert not _model(project).exists()

    def test_geographic_stats_table_provisions_the_model(self, project: Path) -> None:
        _warehouse(project, ["campaigns", "geographic_stats"])

        summary = DbtModelGenerator(project).generate_all_models([_ads()])

        assert summary["errors"] == []
        text = _model(project).read_text(encoding="utf-8")
        assert "source('ads', 'geographic_stats')" in text
        assert "__SOURCE_NAME__" not in text

    @pytest.mark.parametrize("table_present", [False, True])
    def test_an_existing_model_is_never_rewritten(self, project: Path, table_present: bool) -> None:
        _warehouse(project, ["geographic_stats"] if table_present else ["campaigns"])
        existing = _model(project)
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("-- my own version\nselect 1\n", encoding="utf-8")

        DbtModelGenerator(project).generate_all_models([_ads()])

        assert existing.read_text(encoding="utf-8") == "-- my own version\nselect 1\n"


def _dbt(dbt_dir: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, HOME=str(home), DBT_SEND_ANONYMOUS_USAGE_STATS="False")
    dbt = Path(sys.executable).parent / "dbt"
    return subprocess.run(
        [str(dbt), *args, "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )


class TestDbtKeepsWorkingBeforeTheFirstGoogleAdsSync:
    def test_dbt_run_of_another_model_succeeds_after_adding_google_ads(
        self, project: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        dbt_dir = project / "dbt"
        (dbt_dir / "models" / "staging").mkdir(parents=True, exist_ok=True)
        (dbt_dir / "dbt_project.yml").write_text(
            "name: t\nversion: '1.0.0'\nprofile: t\nmodel-paths: [models]\n", encoding="utf-8"
        )
        (dbt_dir / "profiles.yml").write_text(
            "t:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
            f"      path: {project / 'data' / 'warehouse.duckdb'}\n",
            encoding="utf-8",
        )
        (project / "data").mkdir(exist_ok=True)
        (dbt_dir / "models" / "other.sql").write_text("select 1 as x\n", encoding="utf-8")
        home = tmp_path_factory.mktemp("dbt_home")

        apply_source(project, prepare_source(project, "google_ads", "ads", {}))
        DbtModelGenerator(project).generate_all_models([_ads()])

        run = _dbt(dbt_dir, home, "run", "--select", "other")
        assert run.returncode == 0, run.stdout + run.stderr
        assert "depends on a source named" not in run.stdout + run.stderr
