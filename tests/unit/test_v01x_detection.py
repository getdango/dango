"""tests/unit/test_v01x_detection.py

Unit tests for v0.1.x project detection logic.
"""

import re

import pytest
from click import Abort

from dango.cli.utils import check_v01x_project

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_v01x_detected_when_dango_yml_exists(tmp_path, capsys, monkeypatch):
    """check_v01x_project aborts when dango.yml is present."""
    (tmp_path / "dango.yml").write_text("project:\n  name: old\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Abort):
        check_v01x_project()

    captured = capsys.readouterr()
    plain = _strip_ansi(captured.out)
    assert "v0.1.x" in plain
    assert "dango init" in plain
    assert "Back up your data" in plain


def test_v01x_detected_with_project_yml_and_warehouse(tmp_path, capsys, monkeypatch):
    """Detects v0.1.x when .dango/project.yml exists + warehouse but no dango.db."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: old\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "warehouse.duckdb").write_bytes(b"")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Abort):
        check_v01x_project()

    captured = capsys.readouterr()
    plain = _strip_ansi(captured.out)
    assert "v0.1.x" in plain


def test_no_detection_without_dango_yml(tmp_path, monkeypatch):
    """check_v01x_project does nothing when no dango.yml exists."""
    monkeypatch.chdir(tmp_path)
    check_v01x_project()


def test_no_detection_with_v1_project(tmp_path, monkeypatch):
    """check_v01x_project does nothing for a v1 project (.dango/dango.db exists)."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: myproject\n")
    (dango_dir / "dango.db").write_bytes(b"")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "warehouse.duckdb").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    check_v01x_project()


def test_no_detection_fresh_clone_without_warehouse(tmp_path, monkeypatch):
    """No false positive on a cloned v1 project that hasn't synced yet."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: myproject\n")
    # No dango.db AND no warehouse — fresh clone, not v0.1.x
    monkeypatch.chdir(tmp_path)
    check_v01x_project()


def test_v01x_detected_when_both_dango_yml_and_project_yml_exist(tmp_path, capsys, monkeypatch):
    """dango.yml takes precedence even if .dango/project.yml also exists."""
    (tmp_path / "dango.yml").write_text("project:\n  name: old\n")
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: old\n")
    (dango_dir / "dango.db").write_bytes(b"")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(Abort):
        check_v01x_project()
