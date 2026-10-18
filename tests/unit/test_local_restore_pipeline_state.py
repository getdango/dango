"""tests/unit/test_local_restore_pipeline_state.py

A local restore moves aside only the dlt state of the restored project's own pipelines, wherever dlt keeps it.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.factories.backup_shell import make_duckdb

_SOURCES = "version: '1.0'\nsources:\n  - name: {name}\n    type: csv\n    csv:\n      directory: data/uploads\n"


def _restore(tmp_path: Path, project: Path, sources_yml: str | None) -> Any:
    from dango.cli.commands.local_backup import backup_group

    content = tmp_path / "content"
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    if sources_yml is not None:
        (content / ".dango").mkdir()
        (content / ".dango" / "sources.yml").write_text(sources_yml)
    archive = tmp_path / "backup-20260101-000000.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname="backup-20260101-000000")
    with patch("dango.cli.commands.local_backup.require_project_context", return_value=project):
        return CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )


def _pipelines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)
    pipelines = home / ".dlt" / "pipelines"
    for name in ("mine", "other_project"):
        (pipelines / name).mkdir(parents=True)
        (pipelines / name / "state.json").write_text(name)
    return pipelines


def _project(tmp_path: Path, sources_yml: str) -> Path:
    project = tmp_path / "project"
    make_duckdb(project / "data" / "warehouse.duckdb", "live")
    (project / ".dango").mkdir()
    (project / ".dango" / "sources.yml").write_text(sources_yml)
    return project


@pytest.mark.unit
def test_local_restore_moves_only_this_projects_pipelines_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipelines = _pipelines(tmp_path, monkeypatch)
    project = _project(tmp_path, _SOURCES.format(name="mine"))
    result = _restore(tmp_path, project, _SOURCES.format(name="mine"))
    assert result.exit_code == 0, result.output
    assert not (pipelines / "mine").exists(), "live pipeline state left in place"
    kept = list(pipelines.glob("mine.pre-restore-*"))
    assert len(kept) == 1 and (kept[0] / "state.json").read_text() == "mine"
    assert (pipelines / "other_project" / "state.json").read_text() == "other_project"


@pytest.mark.unit
def test_local_restore_honours_dlt_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pipelines(tmp_path, monkeypatch)
    custom = tmp_path / "custom" / "pipelines" / "mine"
    custom.mkdir(parents=True)
    (custom / "state.json").write_text("custom")
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "custom"))
    project = _project(tmp_path, _SOURCES.format(name="mine"))
    result = _restore(tmp_path, project, _SOURCES.format(name="mine"))
    assert result.exit_code == 0, result.output
    assert not custom.exists()
    assert len(list((tmp_path / "custom" / "pipelines").glob("mine.pre-restore-*"))) == 1
    assert (tmp_path / "home" / ".dlt" / "pipelines" / "mine" / "state.json").exists()


@pytest.mark.unit
def test_local_restore_falls_back_to_live_sources_when_restored_ones_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipelines = _pipelines(tmp_path, monkeypatch)
    project = _project(tmp_path, _SOURCES.format(name="mine"))
    result = _restore(tmp_path, project, "sources: [not valid")
    assert result.exit_code == 0, result.output
    assert not (pipelines / "mine").exists()
    assert (pipelines / "other_project").exists()
    assert "sources" in result.output.lower() and "warning" in result.output.lower()
