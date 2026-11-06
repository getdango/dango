"""tests/unit/test_dlt_runner_load_cleanup.py

Row data must not stay on disk under the dlt pipelines dir after a successful load.
Uses real dlt + DuckDB in tmp_path; both sync methods and the staging pipeline are driven.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import dlt
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner

CANARY = "canary-7f3a91c2-row-value"
SOURCE_PY = textwrap.dedent(
    f"""
    import dlt

    @dlt.source
    def c1e_source():
        @dlt.resource(name="items", write_disposition="append")
        def items():
            yield [{{"id": 1, "secret": "{CANARY}"}}, {{"id": 2, "secret": "{CANARY}"}}]

        return items
    """
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Project root with isolated dlt data dir; env restored after the test."""
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / ".dlt"))
    monkeypatch.setenv("DLT_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("LOAD__DELETE_COMPLETED_JOBS", raising=False)
    monkeypatch.delenv("RUNTIME__DLTHUB_TELEMETRY", raising=False)  # restored after the test
    # Opt out of dlt telemetry: no network, and it must not be started in this process
    # (tests/unit/test_egress_allowlist.py expects to be the first to start it).
    monkeypatch.setattr("dango.telemetry.get_dlt_telemetry_state", lambda *a, **k: False)
    (tmp_path / "custom_sources").mkdir()
    (tmp_path / "custom_sources" / "c1e_mod.py").write_text(SOURCE_PY)
    yield tmp_path
    # dlt caches its runtime config (and telemetry flags) on a process-wide RunContext;
    # reset it so later tests (egress allowlist) see a fresh dlt runtime.
    from dlt.common.runtime.telemetry import stop_telemetry

    stop_telemetry()
    dlt.current.run_context().reset_config()


def pipelines_dir(root: Path) -> Path:
    return root / ".dlt" / "pipelines"


def data_files(root: Path) -> list[Path]:
    return [p for p in pipelines_dir(root).rglob("*") if p.is_file()]


def contains_canary(root: Path) -> list[Path]:
    hits = []
    for path in data_files(root):
        raw = path.read_bytes()
        if path.suffix == ".gz":
            raw = gzip.decompress(raw)
        if CANARY.encode() in raw:
            hits.append(path)
    return hits


def run_native(root: Path, name: str = "c1e_native", **kwargs: Any) -> dict[str, Any]:
    source = DataSource(
        name=name,
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="c1e_mod", source_function="c1e_source", pipeline_name=name
        ),
    )
    return DltPipelineRunner(root)._run_dlt_native_source(source, **kwargs)


def run_registry(root: Path, name: str = "c1e_registry", **kwargs: Any) -> dict[str, Any]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("c1e_mod_reg", root / "custom_sources/c1e_mod.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["c1e_mod_reg"] = mod  # dlt needs the source's module to be importable
    spec.loader.exec_module(mod)
    source = DataSource(name=name, type=SourceType.STRIPE)
    runner = DltPipelineRunner(root)
    with (
        patch.object(runner, "_load_dlt_source", side_effect=lambda *a, **k: mod.c1e_source()),
        patch.object(runner, "_build_source_config", return_value={}),
        patch.object(runner, "_inject_oauth_credentials", side_effect=lambda t, kw: kw),
    ):
        return runner._run_dlt_source(source, **kwargs)


RUNNERS = [pytest.param(run_native, id="native"), pytest.param(run_registry, id="registry")]


@pytest.mark.unit
@pytest.mark.parametrize("run", RUNNERS)
def test_no_row_data_on_disk_after_successful_run(env: Path, run: Any) -> None:
    result = run(env)
    assert result["status"] == "success"
    assert result["rows_loaded"] >= 2
    assert data_files(env), "pipeline state should still be on disk"
    assert contains_canary(env) == []


@pytest.mark.unit
@pytest.mark.parametrize("run", RUNNERS)
def test_state_file_survives_cleanup(env: Path, run: Any) -> None:
    run(env)
    assert list(pipelines_dir(env).glob("*/state.json"))


@pytest.mark.unit
@pytest.mark.parametrize("run", RUNNERS)
def test_failed_load_keeps_package_for_recovery(env: Path, run: Any) -> None:
    db = env / "data" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"not a duckdb file" * 100)  # destination cannot be opened
    with pytest.raises(PipelineStepFailed):
        run(env)
    assert contains_canary(env), "failed package must keep its job files for recovery"


@pytest.mark.unit
def test_old_packages_removed_newest_kept(env: Path) -> None:
    run_native(env)
    pipe = pipelines_dir(env) / "c1e_native"
    loaded = pipe / "load" / "loaded"
    for load_id in ("1000.1", "2000.2", "3000.3"):
        job_dir = loaded / load_id / "completed_jobs"
        job_dir.mkdir(parents=True)
        (job_dir / "x.insert_values.gz").write_bytes(gzip.compress(CANARY.encode()))
    (pipe / "load" / "new").mkdir(parents=True, exist_ok=True)
    (pipe / "load" / "normalized").mkdir(parents=True, exist_ok=True)
    run_native(env)
    remaining = sorted(p.name for p in loaded.iterdir())
    assert len(remaining) == 1
    assert float(remaining[0]) > 3000.3  # the newest (this run's) package
    assert (pipe / "load" / "new").is_dir()
    assert (pipe / "load" / "normalized").is_dir()
    assert contains_canary(env) == []


@pytest.mark.unit
def test_cleanup_failure_does_not_fail_sync(env: Path) -> None:
    run_native(env)
    loaded = pipelines_dir(env) / "c1e_native" / "load" / "loaded"
    (loaded / "1000.1").mkdir(parents=True)
    (loaded / "2000.2").mkdir(parents=True)
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name in {"1000.1", "2000.2"}:
            raise OSError("boom")
        real_rmtree(path, *args, **kwargs)

    with patch("shutil.rmtree", side_effect=flaky_rmtree):
        result = run_native(env)
    assert result["status"] == "success"


@pytest.mark.unit
def test_env_override_respected(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOAD__DELETE_COMPLETED_JOBS", "false")
    result = run_native(env)
    assert result["status"] == "success"
    assert os.environ["LOAD__DELETE_COMPLETED_JOBS"] == "false"
    assert contains_canary(env), "operator override must keep completed jobs"


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only (platform guard)")
def test_pipeline_dir_mode_0700(env: Path) -> None:
    run_native(env)
    pipe = pipelines_dir(env) / "c1e_native"
    assert pipe.stat().st_mode & 0o777 == 0o700
    parent_before = (env / ".dlt").stat().st_mode & 0o777
    run_native(env)
    assert (env / ".dlt").stat().st_mode & 0o777 == parent_before


@pytest.mark.unit
def test_staging_pipeline_leaves_no_row_copies(env: Path) -> None:
    """Merge-mode --full-refresh goes through the staging pipeline."""
    source = DataSource(
        name="c1e_native",
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="c1e_mod", source_function="c1e_source", pipeline_name="c1e_native"
        ),
    )
    runner = DltPipelineRunner(env)
    runner._run_dlt_native_source(source)
    with patch.object(runner, "_detect_write_disposition", return_value=False):
        result = runner._run_dlt_native_source(source, full_refresh=True)
    assert result["status"] == "success"
    assert contains_canary(env) == []
    assert dlt  # real dlt in use


# --- state backup (full refresh) must not copy row data -----------------------------


def _runner_after_sync_with_packages(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> DltPipelineRunner:
    """Run one sync that keeps its load packages on disk (operator override)."""
    monkeypatch.setenv("LOAD__DELETE_COMPLETED_JOBS", "false")
    run_native(env)
    assert contains_canary(env), "precondition: packages with row data are on disk"
    return DltPipelineRunner(env)


@pytest.mark.unit
def test_state_backup_honours_dlt_data_dir(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_after_sync_with_packages(env, monkeypatch)
    backup = runner._backup_dlt_state("c1e_native")
    assert backup is not None, "backup must be found under DLT_DATA_DIR, not ~/.dlt"
    assert backup.parent == pipelines_dir(env)


@pytest.mark.unit
def test_state_backup_contains_no_row_data(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_after_sync_with_packages(env, monkeypatch)
    backup = runner._backup_dlt_state("c1e_native")
    assert backup is not None
    files = [p for p in backup.rglob("*") if p.is_file()]
    assert any(p.name == "state.json" for p in files)
    for path in files:
        raw = path.read_bytes()
        raw = gzip.decompress(raw) if path.suffix == ".gz" else raw
        assert CANARY.encode() not in raw, path
    assert not (backup / "load").exists()


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only (platform guard)")
def test_state_backup_dir_mode_0700(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner_after_sync_with_packages(env, monkeypatch)
    backup = runner._backup_dlt_state("c1e_native")
    assert backup is not None
    assert backup.stat().st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_restore_from_state_backup_keeps_pipeline_usable(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner_after_sync_with_packages(env, monkeypatch)
    pipe = pipelines_dir(env) / "c1e_native"
    state_before = (pipe / "state.json").read_text()
    backup = runner._backup_dlt_state("c1e_native")
    assert backup is not None
    (pipe / "state.json").write_text("{corrupted")
    runner._restore_dlt_state(backup)
    assert (pipe / "state.json").read_text() == state_before
    assert not backup.exists()
    assert run_native(env)["status"] == "success"


@pytest.mark.unit
def test_clear_local_pipeline_cache_honours_dlt_data_dir(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner_after_sync_with_packages(env, monkeypatch)
    runner._clear_local_pipeline_cache("c1e_native")
    assert not (pipelines_dir(env) / "c1e_native").exists()


@pytest.mark.unit
def test_legacy_full_copy_backups_removed_after_success(env: Path) -> None:
    run_native(env)
    root = pipelines_dir(env)
    legacy = root / "c1e_native_backup_20200101_000000"
    job_dir = legacy / "load" / "loaded" / "1000.1" / "completed_jobs"
    job_dir.mkdir(parents=True)
    (job_dir / "x.insert_values.gz").write_bytes(gzip.compress(CANARY.encode()))
    (legacy / "state.json").write_text("{}")
    current_format = root / "c1e_native_backup_20200102_000000"
    current_format.mkdir()
    (current_format / "state.json").write_text("{}")
    other_pipeline = root / "other_backup_20200101_000000"
    (other_pipeline / "load").mkdir(parents=True)
    run_native(env)
    assert not legacy.exists()
    assert current_format.exists(), "state-only backups are not row-data copies"
    assert other_pipeline.exists(), "other pipelines' dirs are never touched"
    assert contains_canary(env) == []


@pytest.mark.unit
def test_legacy_backup_kept_when_load_fails(env: Path) -> None:
    run_native(env)
    legacy = pipelines_dir(env) / "c1e_native_backup_20200101_000000"
    (legacy / "load").mkdir(parents=True)
    (env / "data" / "warehouse.duckdb").write_bytes(b"not a duckdb file" * 100)
    with pytest.raises(PipelineStepFailed):
        run_native(env)
    assert legacy.exists()
