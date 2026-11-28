"""tests/unit/test_file_pattern_containment.py

A file source's file_pattern set through the wizard rules or the agent tools stays inside the source directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from dango.config.helpers import load_config, save_config
from dango.config.models import DataSource
from dango.ingestion.csv_loader import CSVLoader
from dango.ingestion.sources.setup_lifecycle import update_source
from dango.ingestion.sources.setup_schema import SourceSetupError, normalize_params
from dango.ingestion.sources.setup_service import create_source

pytestmark = pytest.mark.unit

ESCAPING = [
    "../x/*.csv",
    "../../../../outside/*.json",
    "sub/../../x/*.csv",
    "..",
    "./../x",
    "..\\x\\*.csv",
    "/abs/*.csv",
    "/*",
    "C:\\Users\\fake\\*.csv",
    "C:/fake/*.csv",
    "c:*.csv",
    "\\\\fake-server\\share\\*.csv",
    "//fake-server/share/*",
    "\\rooted\\*.csv",
    ".. /x/*.csv",
    "..\t/x/*.csv",
    "sub/ ../x",
    "sub\\.. \\x",
    "...",
    "x/.../y",
]
FINE = [
    "*",
    "*.csv",
    "data_*.csv",
    "orders_2026-??.csv",
    "2026-*/*.csv",
    "**/*.csv",
    "sub/*.json",
    "./data.csv",
    "a..b.csv",
    "a.b/*.csv",
    "a...b/*.csv",
    ".hidden*",
    "[ab]*.parquet",
]


@pytest.mark.parametrize("source_type", ["local_files", "csv"])
@pytest.mark.parametrize("pattern", ESCAPING)
def test_normalize_params_rejects_a_pattern_that_leaves_the_directory(
    source_type: str, pattern: str
) -> None:
    with pytest.raises(SourceSetupError) as raised:
        normalize_params(source_type, "s", {"directory": "data/uploads/s", "file_pattern": pattern})
    message = " ".join(raised.value.errors)
    assert "file_pattern" in message
    assert "relative" in message and ".." in message


@pytest.mark.parametrize("pattern", FINE)
def test_normalize_params_accepts_ordinary_patterns(pattern: str) -> None:
    params, _ = normalize_params(
        "local_files", "s", {"directory": "data/uploads/s", "file_pattern": pattern}
    )
    assert params["file_pattern"] == pattern


@pytest.fixture
def project(tmp_path: Path, sample_config: Any) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    sample_config.sources.sources = [
        DataSource(
            name="uploads",
            type="local_files",
            local_files={"directory": "data/uploads/uploads", "file_pattern": "*.csv"},
        )
    ]
    save_config(sample_config, root)
    return root


def _sources_text(root: Path) -> str:
    return (root / ".dango" / "sources.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", ESCAPING[:3] + ESCAPING[6:8])
def test_update_source_refuses_an_escaping_pattern_and_writes_nothing(
    project: Path, pattern: str
) -> None:
    before = _sources_text(project)
    with pytest.raises(SourceSetupError) as raised:
        update_source(project, "uploads", {"file_pattern": pattern})
    assert "file_pattern" in " ".join(raised.value.errors)
    assert _sources_text(project) == before


@pytest.mark.parametrize("pattern", ESCAPING[:3] + ESCAPING[6:8])
def test_create_source_refuses_an_escaping_pattern_and_writes_nothing(
    project: Path, pattern: str
) -> None:
    before = _sources_text(project)
    with pytest.raises(SourceSetupError) as raised:
        create_source(
            project,
            "local_files",
            "evil",
            {"directory": "data/uploads/evil", "file_pattern": pattern},
        )
    assert "file_pattern" in " ".join(raised.value.errors)
    assert _sources_text(project) == before
    assert not (project / "data" / "uploads" / "evil").exists()


def test_update_and_create_still_accept_an_ordinary_pattern(project: Path) -> None:
    update_source(project, "uploads", {"file_pattern": "data_*.csv"})
    source = load_config(project).sources.get_source("uploads")
    assert source is not None and source.local_files is not None
    assert source.local_files.file_pattern == "data_*.csv"
    create_source(
        project,
        "local_files",
        "monthly",
        {"directory": "data/uploads/monthly", "file_pattern": "2026-*/*.csv"},
    )
    assert load_config(project).sources.get_source("monthly") is not None


def test_directory_containment_is_unchanged(project: Path) -> None:
    with pytest.raises(SourceSetupError) as raised:
        create_source(project, "local_files", "evil", {"directory": "../outside"})
    assert "must be inside the project" in " ".join(raised.value.errors)
    with pytest.raises(SourceSetupError) as raised:
        update_source(project, "uploads", {"directory": "../outside"})
    assert "must be inside the project" in " ".join(raised.value.errors)


def test_a_sync_after_a_refused_update_loads_nothing_from_outside_the_project(
    tmp_path: Path, project: Path
) -> None:
    """The agent's update is refused; the source then syncs only files inside its directory."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credential.csv").write_text("secret\nFAKE_OUTSIDE_VALUE\n", encoding="utf-8")
    inside = project / "data" / "uploads" / "uploads"
    inside.mkdir(parents=True)
    (inside / "ok.csv").write_text("id\n1\n", encoding="utf-8")

    with pytest.raises(SourceSetupError):
        update_source(project, "uploads", {"file_pattern": "../../../../outside/*.csv"})

    source = load_config(project).sources.get_source("uploads")
    assert source is not None and source.local_files is not None
    db_path = project / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    result = CSVLoader(project, db_path).load("uploads", source.local_files, "raw_uploads")

    assert result["status"] == "success"
    with duckdb.connect(str(db_path), read_only=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw_uploads'"
            ).fetchall()
        ]
        dumped = repr([conn.execute(f'SELECT * FROM raw_uploads."{t}"').fetchall() for t in tables])
    assert "FAKE_OUTSIDE_VALUE" not in dumped
