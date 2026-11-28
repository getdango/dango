"""tests/unit/test_dlt_dataset_names.py

The schema names the runner derives from a configured dataset name (`dlt_dataset_name`, the dlt
merge-staging schema, the staged-refresh schemas) are the names real dlt pipelines create, and the
staging cleanup decision (`staging_schemas_to_drop`) follows the claims of the configured sources.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dlt
import duckdb
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.config.source_checks import staging_collisions
from dango.config.source_datasets import claimed_schemas, dlt_dataset_name, staging_schema_of
from dango.ingestion.staging_cleanup import staging_schemas_to_drop
from tests.factories.dlt_state_projects import FIXTURE_MODULE, entry, isolate_home, project

pytestmark = pytest.mark.unit

DATASETS = ["raw_a", "raw-dash", "Raw_Custom", "CamelCase", "x_", "raw_ev_", "a__b", "UPPER"]


def native(name: str, dataset: str | None = None, *, enabled: bool = True) -> DataSource:
    config = DltNativeConfig(**FIXTURE_MODULE, dataset_name=dataset)
    return DataSource(name=name, type=SourceType.DLT_NATIVE, dlt_native=config, enabled=enabled)


def merge_resource() -> Any:
    @dlt.resource(name="t", write_disposition="merge", primary_key="id")
    def t() -> Any:
        yield [{"id": 1}]

    return t()


def written_schemas(path: Path) -> set[str]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
    finally:
        con.close()
    return {s for (s,) in rows} - {"information_schema", "main", "pg_catalog", "temp", "system"}


@pytest.mark.parametrize("dataset", DATASETS)
def test_the_derived_schema_names_are_the_ones_real_dlt_pipelines_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str
) -> None:
    isolate_home(monkeypatch, tmp_path / "home")
    path = tmp_path / "w.duckdb"
    destination = dlt.destinations.duckdb(credentials=str(path))
    pipelines = str(tmp_path / "pipelines")
    dlt.pipeline(
        "real", destination=destination, dataset_name=dataset, pipelines_dir=pipelines
    ).run(merge_resource())
    written = dlt_dataset_name(dataset)
    refresh = dlt_dataset_name(f"{written}_fullrefresh_staging")
    dlt.pipeline(
        "refresh", destination=destination, dataset_name=refresh, pipelines_dir=pipelines
    ).run(merge_resource())

    expected = {
        written,
        refresh,
        dlt_dataset_name(f"{refresh}_staging"),
        staging_schema_of(native("a", dataset)),
    }
    assert written_schemas(path) == expected
    assert dlt_dataset_name(written) == written  # dlt rewrites a rewritten name to itself


def test_a_file_source_has_no_dlt_staging_schema() -> None:
    csv = DataSource(name="a", type=SourceType.LOCAL_FILES)
    assert staging_schema_of(csv) is None
    assert staging_schema_of(native("a")) == "raw_a_staging"


def test_claimed_schemas_cover_enabled_and_disabled_sources_as_configured_and_as_written() -> None:
    claimed = claimed_schemas([native("a"), native("b", "Raw-Dash", enabled=False)])
    assert claimed == {"raw_a", "raw_dash", "raw-dash"}


def test_staging_collisions_name_the_source_the_other_source_and_the_schema() -> None:
    sources = [native("a"), native("a_staging"), native("b", "Raw-A-Staging"), native("c")]

    assert staging_collisions(sources) == [
        ("a_staging", "a", "raw_a_staging"),
        ("b", "a", "raw_a_staging"),
    ]


class TestStagingSchemasToDrop:
    def test_only_unclaimed_schemas_of_sources_that_synced_qualify(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"), entry("a_staging"), entry("c"))
        schemas = [
            "raw_a_staging",
            "raw_c_staging",
            "raw_gone_staging",
            "raw_a_fullrefresh_staging",
        ]

        assert staging_schemas_to_drop(schemas, ["a", "c"], root, []) == ["raw_c_staging"]

    def test_a_schema_is_matched_by_its_name_without_the_raw_prefix_or_with_it(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, "p", entry("a"))

        assert staging_schemas_to_drop(["a_staging"], ["a"], root, []) == ["a_staging"]
        assert staging_schemas_to_drop(["raw_x_staging"], ["raw_x"], root, []) == ["raw_x_staging"]

    def test_a_claim_is_compared_without_regard_to_case(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"), entry("b", dataset_name="Raw_A_Staging"))

        assert staging_schemas_to_drop(["raw_a_staging"], ["a"], root, []) == []

    def test_nothing_qualifies_without_a_readable_configuration(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        (root / ".dango").mkdir(parents=True)
        (root / ".dango" / "sources.yml").write_text("sources: [oops: \n", encoding="utf-8")

        assert staging_schemas_to_drop(["raw_a_staging"], ["a"], root, [native("a")]) == []
