"""tests/unit/test_runner_staging_cleanup_claims.py

The end-of-sync cleanup of dlt staging schemas (`<dataset>_staging`) must never drop a schema a
configured source loads into: a source called `a_staging` writes `raw_a_staging`, which is also the
name of the merge-staging schema of source `a`. Real dlt pipelines against a local DuckDB through
`run_sync`, a throwaway HOME, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import duckdb
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync
from tests.factories.custom_dataset_projects import ids_in, plant, tables_by_schema
from tests.factories.dlt_state_projects import (
    FIXTURE_MODULE,
    entry,
    ids,
    isolate_home,
    named_source,
    project,
)
from tests.factories.empty_guard_runs import sync
from tests.factories.refresh_cursor_runs import events

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def source_of(name: str, dataset: str | None = None, *, enabled: bool = True) -> DataSource:
    native = DltNativeConfig(**FIXTURE_MODULE, dataset_name=dataset)
    return DataSource(name=name, type=SourceType.DLT_NATIVE, dlt_native=native, enabled=enabled)


def merge_source(name: str, rows: int) -> Any:
    """A merge table with a key: dlt writes its own `<dataset>_staging` copy on every load."""

    @dlt.source(name=name)
    def _src() -> Any:
        yield events(rows, disposition="merge", incremental=False, key=True)

    return _src()


def run(
    root: Path,
    sources: list[DataSource],
    *,
    rows: int = 5,
    merge: tuple[str, ...] = (),
    fail: tuple[str, ...] = (),
) -> dict[str, Any]:
    """`run_sync` of `sources`, each really synced by dlt (append `events`, or merge for `merge`)."""

    def run_source(self: DltPipelineRunner, config: DataSource, *args: Any, **kwargs: Any) -> Any:
        if config.name in fail:
            return {"status": "failed", "source": config.name, "error": "boom"}
        build = merge_source if config.name in merge else named_source
        dataset = config.dlt_native.dataset_name if config.dlt_native else None
        native = {"dataset_name": dataset} if dataset else None
        return sync(self, "native", config.name, build(config.name, rows), native=native)

    generator = MagicMock()
    generator.return_value.generate_all_models.return_value = {}
    with (
        patch.object(DltPipelineRunner, "run_source", autospec=True, side_effect=run_source),
        patch("dango.transformation.generator.DbtModelGenerator", generator),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value=None),
    ):
        return run_sync(root, sources, skip_dbt=True)


class TestASchemaAConfiguredSourceLoadsIntoIsKept:
    def test_a_source_called_x_staging_keeps_its_data_when_x_syncs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        a, a_staging = source_of("a"), source_of("a_staging")
        root = project(tmp_path, "p", entry("a"), entry("a_staging"))
        assert run(root, [a_staging], rows=5)["success_sources"] == ["a_staging"]
        assert ids(root, "a_staging") == upto(5)

        assert run(root, [a], rows=5)["success_sources"] == ["a"]

        assert ids(root, "a_staging") == upto(5)
        assert ids(root, "a") == upto(5)
        assert "Cleaned up" not in capsys.readouterr().out
        assert run(root, [a_staging], rows=8)["success_sources"] == ["a_staging"]
        assert ids(root, "a_staging") == upto(8)  # its cursor still matches its data

    def test_the_schema_of_a_disabled_source_is_kept_too(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"), {**entry("a_staging"), "enabled": False})
        assert run(root, [source_of("a_staging")])["success_sources"] == ["a_staging"]

        assert run(root, [source_of("a")])["success_sources"] == ["a"]

        assert ids(root, "a_staging") == upto(5)

    def test_a_dataset_dlt_rewrites_to_a_staging_schema_is_kept(self, tmp_path: Path) -> None:
        """`Raw-A-Staging` is written as `raw_a_staging`: the claim is the name dlt writes."""
        root = project(tmp_path, "p", entry("a"), entry("b", dataset_name="Raw-A-Staging"))
        assert run(root, [source_of("b", "Raw-A-Staging")])["success_sources"] == ["b"]
        assert ids_in(root, "raw_a_staging") == upto(5)

        assert run(root, [source_of("a")])["success_sources"] == ["a"]

        assert ids_in(root, "raw_a_staging") == upto(5)

    def test_a_source_given_to_the_sync_counts_even_when_it_is_not_in_sources_yml(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, "p", entry("a"))
        summary = run(root, [source_of("a"), source_of("a_staging")])
        assert sorted(summary["success_sources"]) == ["a", "a_staging"]

        assert ids(root, "a_staging") == upto(5)

    def test_nothing_is_dropped_when_the_configured_sources_cannot_be_read(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, "p", entry("a"))
        plant(root, "raw_a_staging")
        (root / ".dango" / "sources.yml").write_text("sources: [not: valid\n", encoding="utf-8")

        assert run(root, [source_of("a")])["success_sources"] == ["a"]

        assert "raw_a_staging" in tables_by_schema(root)


class TestStagingNobodyLoadsIntoIsStillCleanedUp:
    def test_the_merge_staging_schema_dlt_creates_for_a_source_is_dropped(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, "p", entry("a"))
        assert run(root, [source_of("a")], merge=("a",))["success_sources"] == ["a"]
        assert run(root, [source_of("a")], merge=("a",), rows=6)["success_sources"] == ["a"]

        schemas = tables_by_schema(root)
        assert not [s for s in schemas if s.endswith("_staging")]
        assert ids(root, "a") == upto(6)

    def test_a_leftover_staging_schema_of_a_synced_source_is_dropped(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"))
        plant(root, "raw_a_staging")

        assert run(root, [source_of("a")])["success_sources"] == ["a"]

        assert "raw_a_staging" not in tables_by_schema(root)

    def test_nothing_is_dropped_when_the_source_failed(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"))
        plant(root, "raw_a_staging")

        assert run(root, [source_of("a")], fail=("a",))["failed_count"] == 1

        assert "raw_a_staging" in tables_by_schema(root)

    def test_the_staging_schema_of_a_source_that_did_not_sync_stays(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"), entry("b"))
        plant(root, "raw_b_staging")

        assert run(root, [source_of("a")])["success_sources"] == ["a"]

        assert "raw_b_staging" in tables_by_schema(root)

    def test_ordinary_sources_keep_their_data(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p", entry("a"), entry("b"))
        assert sorted(run(root, [source_of("a"), source_of("b")])["success_sources"]) == ["a", "b"]

        con = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
        try:
            assert con.execute('SELECT COUNT(*) FROM "raw_a"."events"').fetchone() == (5,)
            assert con.execute('SELECT COUNT(*) FROM "raw_b"."events"').fetchone() == (5,)
        finally:
            con.close()
