"""tests/unit/test_dlt_state_guard_and_messages.py

`source remove` and `db clean` end a source's local dlt state only when it was created in this project
folder (another project's cursor is never deleted), compare pipeline names without case, say when a
directory stays or cannot be removed, and `db clean` lists the state it will remove before it asks.
Real dlt pipelines against a local DuckDB under a throwaway HOME.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.ingestion.sources.setup_lifecycle import remove_source
from tests.factories.dlt_state_projects import (
    db_clean,
    drop_schema,
    entry,
    ids,
    isolate_home,
    pipeline_dirs,
    pipelines_dir,
    plant_orphan_state,
    project,
    run_sync,
    write_sources,
)
from tests.factories.empty_guard_runs import METHODS

pytestmark = pytest.mark.unit

NAME = "ev"
KEPT_LINE = "Kept the local sync state for 'ev': it was created in another folder"


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def upto(n: int) -> list[int]:
    return list(range(1, n + 1))


def state_file(name: str = NAME) -> Path:
    return pipelines_dir() / name / "state.json"


def orphan(root: Path, name: str) -> None:
    import yaml

    text = (root / ".dango" / "sources.yml").read_text(encoding="utf-8")
    kept = [e for e in yaml.safe_load(text)["sources"] if e["name"] != name]
    write_sources(root, *kept)


@pytest.mark.parametrize("method", METHODS)
class TestAnotherProjectsCursorIsNeverDeleted:
    def test_removing_a_never_synced_source_leaves_the_other_projects_state(
        self, tmp_path: Path, method: str
    ) -> None:
        other = project(tmp_path, "p2", entry(NAME))
        assert run_sync(other, method, NAME, 8)["status"] == "success"
        mine = project(tmp_path, "p1", entry(NAME))

        result = remove_source(mine, NAME, force=True)
        assert NAME in pipeline_dirs()
        assert any(KEPT_LINE in w for w in result.warnings), result.warnings

        assert run_sync(other, method, NAME, 9)["status"] == "success"
        assert ids(other, NAME) == upto(9)

    def test_db_clean_leaves_a_dir_another_project_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        mine = project(tmp_path, "p1", entry(NAME))
        with monkeypatch.context() as own:  # p1's own tables, with a pipeline dir of its own
            own.setenv("DLT_DATA_DIR", str(tmp_path / "p1_dlt"))
            assert run_sync(mine, method, NAME, 5)["status"] == "success"
        other = project(tmp_path, "p2", entry(NAME))
        assert run_sync(other, method, NAME, 5)["status"] == "success"
        orphan(mine, NAME)

        result = db_clean(mine, monkeypatch)
        assert result.exit_code == 0, result.output
        assert NAME in pipeline_dirs()
        assert KEPT_LINE in result.output

        assert run_sync(other, method, NAME, 8)["status"] == "success"
        assert ids(other, NAME) == upto(8)


class TestADirThisProjectCreatedStillGoes:
    def test_through_a_symlinked_project_path(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p1", entry(NAME))
        run_sync(root, "native", NAME, 5)
        drop_schema(root, f"raw_{NAME}")
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)

        remove_source(link, NAME, force=True)
        assert NAME not in pipeline_dirs()

    def test_a_trailing_separator_and_dot_segments_do_not_matter(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p1", entry(NAME))
        run_sync(root, "native", NAME, 5)
        drop_schema(root, f"raw_{NAME}")

        remove_source(Path(str(root) + "/./"), NAME, force=True)
        assert NAME not in pipeline_dirs()


@pytest.mark.parametrize(
    "damage",
    ["missing", "not json", "no _local", "cwd not text", "cwd is another folder", "folder moved"],
)
def test_a_state_file_that_cannot_prove_ownership_keeps_the_dir(
    tmp_path: Path, damage: str
) -> None:
    root = project(tmp_path, "p1", entry(NAME))
    run_sync(root, "native", NAME, 5)
    drop_schema(root, f"raw_{NAME}")
    path = state_file()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if damage == "missing":
        path.unlink()
    elif damage == "not json":
        path.write_text("{ not json", encoding="utf-8")
    else:
        if damage == "no _local":
            del data["_local"]
        elif damage == "cwd not text":
            data["_local"]["initial_cwd"] = 7
        elif damage == "cwd is another folder":
            data["_local"]["initial_cwd"] = str(tmp_path)
        else:
            data["_local"]["initial_cwd"] = str(tmp_path / "moved_away")
        path.write_text(json.dumps(data), encoding="utf-8")

    remove_source(root, NAME, force=True)
    assert NAME in pipeline_dirs()


def test_a_name_that_differs_only_in_case_from_a_configured_source_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS and Windows `KEPT` and `kept` are one directory; on Linux a copy stands in for it."""
    root = project(tmp_path, "p1", entry("kept"))
    run_sync(root, "native", "kept", 5)
    if not (pipelines_dir() / "KEPT").exists():
        shutil.copytree(pipelines_dir() / "kept", pipelines_dir() / "KEPT")
    plant_orphan_state(root, "raw_evil", "KEPT")

    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Cleared the local sync state for 'KEPT'" not in result.output
    assert (pipelines_dir() / "kept" / "state.json").exists()
    assert (pipelines_dir() / "KEPT").exists()
    assert run_sync(root, "native", "kept", 8)["status"] == "success"
    assert ids(root, "kept") == upto(8)


def test_a_configured_name_with_capitals_is_matched_without_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured name is `MixedCase`; an orphaned state table names it `mixedcase`."""
    native = {"pipeline_name": "MixedCase"}
    root = project(tmp_path, "p1", entry("x", **native))
    run_sync(root, "native", "x", 5, native=native)
    if not (pipelines_dir() / "mixedcase").exists():
        shutil.copytree(pipelines_dir() / "MixedCase", pipelines_dir() / "mixedcase")
    plant_orphan_state(root, "raw_evil", "mixedcase")

    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Cleared the local sync state" not in result.output
    assert (pipelines_dir() / "MixedCase" / "state.json").exists()
    assert (pipelines_dir() / "mixedcase" / "state.json").exists()


class TestWhenTheDirCannotBeRemoved:
    def test_remove_says_the_local_state_is_still_there(self, tmp_path: Path) -> None:
        root = project(tmp_path, "p1", entry(NAME))
        run_sync(root, "native", NAME, 5)
        drop_schema(root, f"raw_{NAME}")
        with patch("shutil.rmtree", side_effect=OSError("busy")):
            result = remove_source(root, NAME, force=True)
        assert NAME in pipeline_dirs()
        assert any(
            "Could not clear the local sync state for 'ev' (OSError)" in w
            and "dango sync ev --full-refresh" in w
            for w in result.warnings
        ), result.warnings

    def test_db_clean_says_the_local_state_is_still_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = project(tmp_path, "p1", entry(NAME))
        run_sync(root, "native", NAME, 5)
        orphan(root, NAME)
        with patch("shutil.rmtree", side_effect=OSError("busy")):
            result = db_clean(root, monkeypatch)
        assert result.exit_code == 0, result.output
        assert result.output.count("Could not clear the local sync state for 'ev' (OSError)") == 1
        assert "dango sync ev --full-refresh" in result.output


class TestDbCleanListsTheStateItWillRemove:
    def test_before_it_asks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = project(tmp_path, "p1", entry(NAME), entry("kept"))
        run_sync(root, "native", NAME, 5)
        run_sync(root, "native", "kept", 5)
        orphan(root, NAME)

        result = db_clean(root, monkeypatch, answer="n\n")
        out = result.output
        assert out.count("Local sync state that will also be removed:") == 1
        assert out.index("Local sync state that will also be removed:") < out.index(
            "Remove these orphaned tables?"
        )
        assert "  - ev\n" in out
        assert "  - kept" not in out
        assert pipeline_dirs() == ["ev", "kept"]

    def test_with_yes_the_list_comes_before_the_drops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = project(tmp_path, "p1", entry(NAME))
        run_sync(root, "native", NAME, 5)
        orphan(root, NAME)
        out = db_clean(root, monkeypatch).output
        assert out.index("Local sync state that will also be removed:") < out.index("Dropped ")
        assert pipeline_dirs() == []

    def test_a_dir_that_will_be_kept_is_not_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mine = project(tmp_path, "p1", entry(NAME))
        with monkeypatch.context() as own:
            own.setenv("DLT_DATA_DIR", str(tmp_path / "p1_dlt"))
            run_sync(mine, "native", NAME, 5)
        other = project(tmp_path, "p2", entry(NAME))
        run_sync(other, "native", NAME, 5)
        orphan(mine, NAME)
        out = db_clean(mine, monkeypatch, answer="n\n").output
        assert "Local sync state that will also be removed" not in out
