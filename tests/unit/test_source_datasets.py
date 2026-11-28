"""tests/unit/test_source_datasets.py

The one place that says which schemas and tables a configured source writes to: it agrees with the
runner's and `source remove`'s own resolution, and `source remove` keeps the sync state of a source
whose data sits in a schema dlt rewrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands.source import _resolve_pipeline_and_dataset_name
from dango.config.models import DangoConfig, DataSource, DltNativeConfig, SourcesConfig, SourceType
from dango.config.source_datasets import schemas_of, tables_of
from dango.ingestion.pipeline_cleanup import dataset_name_of, pipeline_name_of
from dango.ingestion.sources.setup_lifecycle import remove_source
from dango.utils.db_health import build_schema_table_mapping
from tests.factories.custom_dataset_projects import sources_of, synced_into
from tests.factories.dlt_state_projects import isolate_home, pipeline_dirs, project

pytestmark = pytest.mark.unit


def native(name: str, **fields: str) -> DataSource:
    config = DltNativeConfig(source_module="m", source_function="f", **fields)
    return DataSource(name=name, type=SourceType.DLT_NATIVE, dlt_native=config)


def stripe(name: str = "shop", endpoints: list[str] | None = None) -> DataSource:
    return DataSource.model_validate(
        {"name": name, "type": "stripe", "stripe": {"endpoints": endpoints}}
    )


def files(type_: str, name: str = "sales") -> DataSource:
    return DataSource.model_validate(
        {"name": name, "type": type_, type_: {"directory": "data/uploads"}}
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (native("b"), ["raw_b"]),
        (native("b", dataset_name="raw_custom"), ["raw_custom"]),
        (native("b", dataset_name="Raw_Custom"), ["raw_custom"]),
        (native("b", dataset_name="raw-dash"), ["raw_dash", "raw-dash"]),
        (native("a_"), ["raw_ax", "raw_a_"]),
        (native("b", dataset_name="  "), ["  "]),
        (stripe(), ["raw_shop"]),
        (files("csv"), ["raw_sales"]),
        (files("local_files"), ["raw_sales"]),
    ],
)
def test_schemas_of(source: DataSource, expected: list[str]) -> None:
    assert schemas_of(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        native("b"),
        native("b", dataset_name="raw_custom"),
        native("b", pipeline_name="pb"),
        stripe(),
        files("csv"),
    ],
)
def test_the_runner_source_remove_and_the_scan_resolve_the_same_dataset(
    source: DataSource,
) -> None:
    runner_pipeline, runner_dataset = _resolve_pipeline_and_dataset_name(source)
    assert dataset_name_of(source) == runner_dataset
    assert runner_dataset.casefold() in schemas_of(source)
    if source.type != SourceType.CSV:
        assert pipeline_name_of(source) == runner_pipeline
    else:
        assert pipeline_name_of(source) is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (stripe(), None),
        (stripe(endpoints=[]), None),
        (
            stripe(endpoints=["Customer", "InvoiceItem"]),
            {"customer", "invoiceitem", "invoice_item"},
        ),
        (native("b"), None),
        (
            DataSource.model_validate(
                {
                    "name": "api",
                    "type": "rest_api",
                    "rest_api": {"base_url": "https://x.invalid", "endpoints": [{"name": "u"}]},
                }
            ),
            None,
        ),
    ],
)
def test_tables_of(source: DataSource, expected: set[str] | None) -> None:
    assert tables_of(source) == expected


def test_source_remove_keeps_the_sync_state_while_data_sits_in_a_rewritten_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_home(monkeypatch, tmp_path / "home")
    root = project(tmp_path, "p1", *sources_of(b="Raw-Dash"))
    synced_into(root, "b", 5, "Raw-Dash")

    remove_source(root, "b", force=True)

    assert pipeline_dirs() == ["b"]


def test_the_table_mapping_gives_a_shared_schema_to_every_table_when_one_source_lists_none() -> (
    None
):
    sources = [stripe("shop", endpoints=["customer"]), native("b", dataset_name="raw_shop")]
    config = DangoConfig.model_construct(sources=SourcesConfig(sources=sources))
    schema_to_tables, source_to_schema = build_schema_table_mapping(config)
    assert schema_to_tables == {"raw_shop": set()}
    assert source_to_schema == {"shop": "raw_shop", "b": "raw_shop"}
    only_listed = DangoConfig.model_construct(sources=SourcesConfig(sources=sources[:1]))
    assert build_schema_table_mapping(only_listed)[0] == {"raw_shop": {"customer"}}
