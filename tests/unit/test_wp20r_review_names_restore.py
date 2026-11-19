"""tests/unit/test_wp20r_review_names_restore.py

Unicode and spaced pipeline names still work, restore targets the right pipeline, full refresh stops when the cache cannot be cleared.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.config.models import DltNativeConfig
from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.utils.safe_names import contained_child

_PIPELINES = "dango.ingestion.dlt_runner._dlt_pipelines_dir"


def _runner() -> DltPipelineRunner:
    return DltPipelineRunner.__new__(DltPipelineRunner)


def _state(pipelines: Path, name: str, cursor: str) -> Path:
    path = pipelines / name / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")
    return path


@pytest.mark.unit
@pytest.mark.parametrize("name", ["café", "Tiny Pipeline", "数据", "a.b-c_d"])
def test_names_dlt_accepts_are_backed_up_and_cleared(tmp_path: Path, name: str) -> None:
    pipelines = tmp_path / "pipelines"
    _state(pipelines, name, "c1")
    with patch(_PIPELINES, return_value=pipelines):
        backup = _runner()._backup_dlt_state(name)
        assert backup is not None and (backup / "state.json").exists()
        _runner()._clear_local_pipeline_cache(name)
    assert not (pipelines / name).exists()


@pytest.mark.unit
@pytest.mark.parametrize("name", ["café", "Tiny Pipeline", "数据"])
def test_config_accepts_the_names_dlt_accepts(name: str) -> None:
    assert DltNativeConfig(pipeline_name=name).pipeline_name == name


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../..", "a/b", "a\\b", "/abs/x", "", ".", ".."])
def test_a_single_path_component_is_required(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        contained_child(tmp_path, name)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../..", "a/b", "a\\b", "/abs/x", "..", "."])
def test_config_refuses_names_that_are_not_a_single_component(name: str) -> None:
    with pytest.raises(ValueError):
        DltNativeConfig(pipeline_name=name)


@pytest.mark.unit
def test_restoring_a_backup_of_a_name_containing_backup_targets_that_pipeline(
    tmp_path: Path,
) -> None:
    pipelines = tmp_path / "pipelines"
    orders = _state(pipelines, "orders", "orders-cursor")
    v2 = _state(pipelines, "orders_backup_v2", "v2-before")
    with patch(_PIPELINES, return_value=pipelines):
        backup = _runner()._backup_dlt_state("orders_backup_v2")
        v2.write_text(json.dumps({"cursor": "v2-partial"}), encoding="utf-8")
        _runner()._restore_dlt_state(backup)
    assert json.loads(orders.read_text(encoding="utf-8")) == {"cursor": "orders-cursor"}
    assert json.loads(v2.read_text(encoding="utf-8")) == {"cursor": "v2-before"}


@pytest.mark.unit
def test_restoring_the_short_name_leaves_the_longer_named_pipeline_alone(
    tmp_path: Path,
) -> None:
    pipelines = tmp_path / "pipelines"
    orders = _state(pipelines, "orders", "orders-before")
    v2 = _state(pipelines, "orders_backup_v2", "v2-cursor")
    with patch(_PIPELINES, return_value=pipelines):
        backup = _runner()._backup_dlt_state("orders")
        orders.write_text(json.dumps({"cursor": "orders-partial"}), encoding="utf-8")
        _runner()._restore_dlt_state(backup)
    assert json.loads(orders.read_text(encoding="utf-8")) == {"cursor": "orders-before"}
    assert json.loads(v2.read_text(encoding="utf-8")) == {"cursor": "v2-cursor"}


@pytest.mark.unit
def test_restore_refuses_a_directory_that_is_not_one_of_our_backups(tmp_path: Path) -> None:
    pipelines = tmp_path / "pipelines"
    keep = _state(pipelines, "orders", "keep")
    stray = pipelines / "orders_backup_notes"
    stray.mkdir()
    (stray / "state.json").write_text('{"cursor": "stray"}', encoding="utf-8")
    with patch(_PIPELINES, return_value=pipelines):
        _runner()._restore_dlt_state(stray)
    assert json.loads(keep.read_text(encoding="utf-8")) == {"cursor": "keep"}
    assert stray.exists()


@pytest.mark.unit
def test_failed_cache_clear_raises_by_default(tmp_path: Path) -> None:
    pipelines = tmp_path / "pipelines"
    _state(pipelines, "orders", "c")
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch("dango.ingestion.dlt_runner.console"),
        pytest.raises(RuntimeError, match="file in use"),
    ):
        _runner()._clear_local_pipeline_cache("orders")


@pytest.mark.unit
def test_cleanup_after_a_run_still_only_warns(tmp_path: Path) -> None:
    pipelines = tmp_path / "pipelines"
    _state(pipelines, "orders", "c")
    console = MagicMock()
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch("dango.ingestion.dlt_runner.console", console),
    ):
        _runner()._clear_local_pipeline_cache("orders", strict=False)
    assert console.print.called


@pytest.mark.unit
def test_full_refresh_stops_before_loading_when_the_cache_cannot_be_cleared(
    tmp_path: Path,
) -> None:
    pipelines = tmp_path / "pipelines"
    _state(pipelines, "test_pipeline", "stale")
    runner = _runner()
    runner.project_root = tmp_path
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True)
    runner._current_oauth_warning = None
    runner._detect_write_disposition = MagicMock(return_value=True)  # replace mode
    runner._run_extract_with_retry = MagicMock()
    runner._extract_load_stats = MagicMock()
    config = MagicMock()
    config.name = "test_source"
    config.type.value = "dlt_native"
    config.dlt_native.source_module = "test_module"
    config.dlt_native.source_function = "test_func"
    config.dlt_native.pipeline_name = "test_pipeline"
    config.dlt_native.dataset_name = "raw_test"
    config.dlt_native.source_args = {}
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch("dango.ingestion.dlt_runner.console"),
        patch("dango.ingestion.dlt_runner.dlt"),
        patch("dango.ingestion.dlt_runner.importlib"),
        patch("os.chdir"),
        patch("os.getcwd", return_value="/tmp"),
        pytest.raises(RuntimeError, match="stopped before loading"),
    ):
        runner._run_dlt_native_source(config, full_refresh=True)
    runner._run_extract_with_retry.assert_not_called()
