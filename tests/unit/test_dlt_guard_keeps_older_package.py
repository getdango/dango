"""tests/unit/test_dlt_guard_keeps_older_package.py

The replace-mode pre-load guard drops the packages of the run it blocks. It must not drop an older
package an earlier failed load left behind: that package's rows are covered by the pipeline state the
earlier failure kept, so dropping it would lose those rows for good. Also the timeout message of
`run_source` says whether the pipeline state was kept or restored. Real dlt + DuckDB, both methods.
"""

from __future__ import annotations

import importlib
import types
from collections import Counter
from typing import Any
from unittest.mock import patch

import dlt
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.exceptions import SyncTimeoutError
from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, ids, sync

pytestmark = pytest.mark.unit

NAME = "mx"


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def source(events: int, dims: int) -> Any:
    """`events` (append, incremental cursor) with ids 1..events; `dims` (replace) with `dims` rows."""

    @dlt.resource(name="events", write_disposition="append")
    def ev(updated_at: Any = dlt.sources.incremental("updated_at", initial_value=0)) -> Any:
        for i in range(1, events + 1):
            yield {"id": i, "updated_at": i}

    @dlt.resource(name="dims", write_disposition="replace")
    def dm() -> Any:
        for i in range(1, dims + 1):
            yield {"id": i}

    @dlt.source(name=NAME)
    def _src() -> Any:
        return [ev, dm]

    return _src()


def fail_load(runner: DltPipelineRunner, method: str, src: Any) -> None:
    with (
        patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("lf")),
        pytest.raises(RuntimeError, match="lf"),
    ):
        sync(runner, method, NAME, src)


def blocked_by_guard(runner: DltPipelineRunner, method: str, src: Any) -> None:
    result = sync(runner, method, NAME, src)
    assert result["status"] == "failed"
    assert "truncate" in result["error"]


@pytest.mark.parametrize("method", METHODS)
class TestGuardAfterAFailedLoad:
    def test_rows_of_the_older_package_survive_the_guard(self, tmp_path: Any, method: str) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, NAME, source(5, 3))["status"] == "success"
        fail_load(runner, method, source(8, 3))
        blocked_by_guard(runner, method, source(8, 0))

        assert sync(runner, method, NAME, source(10, 3))["status"] == "success"
        assert ids(runner, NAME, "events") == list(range(1, 11))
        assert ids(runner, NAME, "dims") == [1, 2, 3]

    def test_guard_without_an_older_package_behaves_as_before(
        self, tmp_path: Any, method: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, NAME, source(5, 3))["status"] == "success"
        blocked_by_guard(runner, method, source(8, 0))
        assert not dlt.attach(NAME).has_pending_data

        assert sync(runner, method, NAME, source(10, 3))["status"] == "success"
        assert Counter(ids(runner, NAME, "events")) == Counter(range(1, 11))
        assert ids(runner, NAME, "dims") == [1, 2, 3]


def test_dlt_still_offers_the_package_storages_the_guard_relies_on(tmp_path: Any) -> None:
    pipeline = dlt.pipeline(
        pipeline_name="storages",
        destination=dlt.destinations.duckdb(credentials=str(tmp_path / "w.duckdb")),
    )
    assert hasattr(pipeline._get_load_storage().normalized_packages, "delete_package")
    assert hasattr(pipeline._get_normalize_storage().extracted_packages, "delete_package")


def native_config() -> DataSource:
    return DataSource(
        name=NAME,
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="fixture_module", source_function="make", function_kwargs={}
        ),
    )


def run_source_with_timeout(tmp_path: Any, where: str) -> str:
    """Everything printed by `run_source` when a timeout fires `where` ("load" or "extract")."""
    runner = DltPipelineRunner(tmp_path)
    assert sync(runner, "native", NAME, source(5, 3))["status"] == "success"
    module = types.SimpleNamespace(make=lambda: source(8, 3))
    real_import = importlib.import_module

    def _import(name: str, *args: Any, **kwargs: Any) -> Any:
        return module if name == "fixture_module" else real_import(name, *args, **kwargs)

    patched = (
        patch.object(DltPipelineRunner, "_load_with_lock", side_effect=SyncTimeoutError("t"))
        if where == "load"
        else patch.object(
            DltPipelineRunner, "_run_extract_with_retry", side_effect=SyncTimeoutError("t")
        )
    )
    with (
        patch("dango.ingestion.dlt_runner.importlib.import_module", side_effect=_import),
        patch("dango.ingestion.dlt_runner.console") as printed,
        patched,
    ):
        result = runner.run_source(native_config())
    assert result["status"] == "failed"
    return "\n".join(" ".join(str(a) for a in c.args) for c in printed.print.call_args_list)


class TestTimeoutMessage:
    def test_a_timeout_after_extraction_says_the_state_was_kept(self, tmp_path: Any) -> None:
        output = run_source_with_timeout(tmp_path, "load")
        assert "Pipeline state was kept with the extracted data" in output
        assert "has been restored" not in output

    def test_a_timeout_before_a_package_exists_says_the_state_was_restored(
        self, tmp_path: Any
    ) -> None:
        output = run_source_with_timeout(tmp_path, "extract")
        assert "Pipeline state has been restored" in output
        assert "was kept with the extracted data" not in output
