"""tests/unit/test_db_clean_hostile_names_and_status.py

`dango db clean` quotes every schema and table name it drops (a crafted table name cannot reach
another table), and `dango db status` classifies raw tables the way `dango db clean` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.custom_dataset_projects import (
    db_status,
    ids_in,
    plant,
    sources_of,
    tables_by_schema,
)
from tests.factories.dlt_state_projects import db_clean, entry, isolate_home, project

pytestmark = pytest.mark.unit

HOSTILE = [
    "a; DROP TABLE raw_keep.events; --",
    "my table",
    "select",
    'q"uote',
    "Ünï",
    "x'; DROP SCHEMA raw_keep CASCADE; --",
]


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


@pytest.mark.parametrize("name", HOSTILE)
def test_a_crafted_orphan_table_name_never_reaches_a_configured_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    root = project(tmp_path, "p1", entry("keep"))
    plant(root, "raw_keep", ["events"])
    plant(root, "raw_gone", [name, "plain"])

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert ids_in(root, "raw_keep") == [1]
    assert tables_by_schema(root).get("raw_gone", []) == []


def test_a_crafted_orphan_schema_name_never_reaches_a_configured_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("keep"))
    plant(root, "raw_keep", ["events"])
    plant(root, 'raw_x"; DROP TABLE raw_keep.events; --', ["t"])

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert ids_in(root, "raw_keep") == [1]


def test_status_does_not_call_a_kept_schema_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_new"))
    plant(root, "raw_old", ["events"], state="b")
    plant(root, "raw_odd", ["events"], state_sql="SELECT 1 AS something_else")

    result = db_status(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Orphaned Tables" not in result.output
    assert "Run 'dango db clean'" not in result.output
    assert "No orphaned tables found" in result.output
    assert "Kept raw_old" in result.output


def test_status_lists_what_clean_would_drop_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    plant(root, "raw_custom", ["events"])
    plant(root, "raw_ghost", ["gone_table"], state="ghost")

    result = db_status(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Orphaned Tables" in result.output
    assert "gone_table" in result.output
    assert "Run 'dango db clean' to remove them" in result.output
    orphan_part = result.output.split("Orphaned Tables", 1)[1]
    assert "raw_custom" not in orphan_part
