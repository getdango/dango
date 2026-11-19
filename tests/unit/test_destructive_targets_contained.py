"""tests/unit/test_destructive_targets_contained.py

Cleanup targets built from a configured name stay inside the directory they belong to, and
retention never deletes the copy the current run just made.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dango.cli.commands.upgrade import _pre_upgrade_safety_copy
from dango.config.models import DltNativeConfig
from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.platform.local.network import NginxManager

_BAD_NAMES = ["../victim", "../../victim", "..", ".", "/abs/victim", "a/b", "a\\b", ""]


def _runner() -> DltPipelineRunner:
    return DltPipelineRunner.__new__(DltPipelineRunner)


def _pipelines(tmp_path: Path) -> tuple[Path, Path]:
    pipelines = tmp_path / "dlt" / "pipelines"
    pipelines.mkdir(parents=True)
    victim = tmp_path / "dlt" / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    (pipelines / "neighbour").mkdir()
    (pipelines / "neighbour" / "state.json").write_text("{}", encoding="utf-8")
    return pipelines, victim


@pytest.mark.unit
@pytest.mark.parametrize("name", _BAD_NAMES[:4])
def test_clearing_a_pipeline_cache_refuses_names_that_leave_the_pipelines_dir(
    tmp_path: Path, name: str
) -> None:
    pipelines, victim = _pipelines(tmp_path)
    with (
        patch("dango.ingestion.dlt_runner._dlt_pipelines_dir", return_value=pipelines),
        pytest.raises(ValueError),
    ):
        _runner()._clear_local_pipeline_cache(name)
    assert (victim / "keep.txt").exists()
    assert (pipelines / "neighbour" / "state.json").exists()


@pytest.mark.unit
def test_backing_up_pipeline_state_refuses_a_traversal_name(tmp_path: Path) -> None:
    pipelines, victim = _pipelines(tmp_path)
    (victim / "state.json").write_text("{}", encoding="utf-8")
    with (
        patch("dango.ingestion.dlt_runner._dlt_pipelines_dir", return_value=pipelines),
        pytest.raises(ValueError),
    ):
        _runner()._backup_dlt_state("../victim")
    assert [p.name for p in pipelines.parent.iterdir() if "backup" in p.name] == []


@pytest.mark.unit
def test_clearing_a_normal_pipeline_cache_removes_only_that_pipeline(tmp_path: Path) -> None:
    pipelines, victim = _pipelines(tmp_path)
    (pipelines / "orders").mkdir()
    (pipelines / "orders" / "state.json").write_text("{}", encoding="utf-8")
    with patch("dango.ingestion.dlt_runner._dlt_pipelines_dir", return_value=pipelines):
        _runner()._clear_local_pipeline_cache("orders")
    assert not (pipelines / "orders").exists()
    assert (pipelines / "neighbour" / "state.json").exists()
    assert (victim / "keep.txt").exists()


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../..", "../x", "/abs/x", "a/b"])
def test_configured_pipeline_name_must_be_a_plain_name(name: str) -> None:
    with pytest.raises(ValueError):
        DltNativeConfig(pipeline_name=name)


def _nginx(tmp_path: Path) -> tuple[NginxManager, Path]:
    sites = tmp_path / "shared" / "sites"
    sites.mkdir(parents=True)
    config = SimpleNamespace(
        sites_dir=sites, shared_nginx_dir=tmp_path / "shared", logs_dir=tmp_path / "logs"
    )
    return NginxManager(config), sites  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../victim", "../../victim", "a/b"])
def test_removing_a_site_config_refuses_names_that_leave_the_sites_dir(
    tmp_path: Path, name: str
) -> None:
    manager, sites = _nginx(tmp_path)
    victim = tmp_path / "victim.conf"
    victim.write_text("keep", encoding="utf-8")
    (sites / "victim.conf").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        manager.remove_project_config(name)
    assert victim.exists()
    assert (sites / "victim.conf").exists()


@pytest.mark.unit
def test_removing_a_site_config_removes_only_that_site(tmp_path: Path) -> None:
    manager, sites = _nginx(tmp_path)
    (sites / "mine.conf").write_text("x", encoding="utf-8")
    (sites / "other.conf").write_text("x", encoding="utf-8")
    manager.remove_project_config("mine")
    assert not (sites / "mine.conf").exists()
    assert (sites / "other.conf").exists()


@pytest.mark.unit
def test_safety_copy_retention_never_deletes_the_copy_just_made(tmp_path: Path) -> None:
    backups = tmp_path / ".dango" / "backups"
    backups.mkdir(parents=True)
    for stamp in ("29990101T000000Z", "29990102T000000Z", "29990103T000000Z"):
        (backups / f"pre-upgrade-{stamp}").mkdir()
    dest = _pre_upgrade_safety_copy(tmp_path)
    assert dest.is_dir(), "the safety copy reported to the user was pruned"
