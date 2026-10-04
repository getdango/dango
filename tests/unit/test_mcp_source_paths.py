"""tests/unit/test_mcp_source_paths.py

MCP create_source file_path handling (home expansion) and the local_files setup-schema hint.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import save_config

CSV = "id,amount\n1,10\n2,20\n"


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    return root


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.mark.unit
def test_create_source_file_path_expands_tilde(project: Path, fake_home: Path) -> None:
    (fake_home / "orders.csv").write_text(CSV)
    result = mcp_sources.create_source("local_files", "orders", file_path="~/orders.csv")
    assert result["status"] == "created", result
    assert (project / "data" / "uploads" / "orders" / "orders.csv").read_text() == CSV


@pytest.mark.unit
def test_create_source_file_path_tilde_missing_file_names_real_path(
    project: Path, fake_home: Path
) -> None:
    result = mcp_sources.create_source("local_files", "orders", file_path="~/nope.csv")
    assert "error" in result
    assert str(fake_home / "nope.csv") in result["error"]
    assert "~" not in result["error"]
    assert str(project) not in result["error"]


@pytest.mark.unit
def test_create_source_relative_file_path_still_resolves_from_project(project: Path) -> None:
    (project / "inbox").mkdir()
    (project / "inbox" / "orders.csv").write_text(CSV)
    result = mcp_sources.create_source("local_files", "orders", file_path="inbox/orders.csv")
    assert result["status"] == "created", result


@pytest.mark.unit
def test_local_files_setup_schema_tells_agents_to_omit_directory(project: Path) -> None:
    schema = mcp_sources.get_source_setup_schema("local_files")
    assert "omit 'directory'" in schema["note"]
    assert "file_path" in schema["note"]
    assert "use_instead" not in schema


@pytest.mark.unit
def test_other_source_schemas_have_no_local_files_note(project: Path) -> None:
    assert "note" not in mcp_sources.get_source_setup_schema("stripe")
