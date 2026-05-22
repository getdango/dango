"""tests/unit/test_v01x_detection.py

Unit tests for v0.1.x project detection logic.
"""

import re

import pytest

from dango.cli.utils import check_v01x_project

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_v01x_detected_when_dango_yml_exists(tmp_path, capsys, monkeypatch):
    """check_v01x_project exits with code 1 when dango.yml is present."""
    (tmp_path / "dango.yml").write_text("project:\n  name: old\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match="1"):
        check_v01x_project()

    captured = capsys.readouterr()
    plain = _strip_ansi(captured.out)
    assert "v0.1.x" in plain
    assert "dango init" in plain
    assert "Back up your data" in plain


def test_no_detection_without_dango_yml(tmp_path, monkeypatch):
    """check_v01x_project does nothing when no dango.yml exists."""
    monkeypatch.chdir(tmp_path)
    # Should not raise
    check_v01x_project()


def test_no_detection_with_v1_project(tmp_path, monkeypatch):
    """check_v01x_project does nothing for a v1 project (.dango/project.yml)."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: myproject\n")
    monkeypatch.chdir(tmp_path)
    # Should not raise
    check_v01x_project()
