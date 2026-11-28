"""tests/unit/test_validate_dataset_staging_collision.py

`dango validate` warns when a source loads into the schema dlt uses for the merge-staging tables of
another source (`raw_a_staging` for source `a`): a merge load of `a` writes its same-named tables
there, so the two sources overwrite each other. The warning names both sources and the fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.cli.validate import ProjectValidator

pytestmark = pytest.mark.unit

HEAVY = (
    "_check_scaffold",
    "_check_dbt_setup",
    "_check_model_sql_portability",
    "_check_script_paths",
    "_check_database",
    "_check_dependencies",
    "_check_permissions",
    "_check_tracked_secrets",
)


def native(name: str, **extra: Any) -> dict[str, Any]:
    block = {"source_module": "m", "source_function": "f", **extra}
    return {"name": name, "type": "dlt_native", "dlt_native": block}


def local(name: str) -> dict[str, Any]:
    return {"name": name, "type": "local_files", "local_files": {"directory": f"data/{name}"}}


def validated(root: Path, sources: list[dict[str, Any]]) -> ProjectValidator:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    (root / ".dango" / "project.yml").write_text(
        yaml.safe_dump({"project": {"name": "t", "created_by": "x@example.com", "purpose": "t"}}),
        encoding="utf-8",
    )
    (root / ".dango" / "sources.yml").write_text(
        yaml.safe_dump({"version": "1.0", "sources": sources}), encoding="utf-8", newline="\n"
    )
    validator = ProjectValidator(root)
    with patch.multiple(validator, **{name: MagicMock() for name in HEAVY}):
        validator.validate_all(display=False, include_connectivity=False)
    return validator


def collisions(validator: ProjectValidator) -> list[Any]:
    return [r for r in validator.results if r.status == "warn" and "raw_a_staging" in r.message]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def test_a_source_named_x_staging_next_to_source_x_warns_naming_both(tmp_path: Path) -> None:
    validator = validated(tmp_path, [native("a"), native("a_staging")])

    rows = collisions(validator)

    assert len(rows) == 1
    message = " ".join(rows[0].message.split())
    assert "a_staging" in message and "'a'" in message
    assert "merge" in message and "staging" in message
    assert "rename" in message.lower()


def test_a_custom_dataset_name_that_is_the_staging_schema_of_another_source_warns(
    tmp_path: Path,
) -> None:
    validator = validated(tmp_path, [native("a"), native("b", dataset_name="raw_a_staging")])

    rows = collisions(validator)

    assert len(rows) == 1
    assert "'b'" in rows[0].message and "'a'" in rows[0].message


def test_a_dataset_name_dlt_rewrites_to_that_schema_warns_too(tmp_path: Path) -> None:
    validator = validated(tmp_path, [native("a"), native("b", dataset_name="Raw-A-Staging")])

    assert len(collisions(validator)) == 1


def test_a_disabled_source_still_counts(tmp_path: Path) -> None:
    validator = validated(tmp_path, [native("a"), {**native("a_staging"), "enabled": False}])

    assert len(collisions(validator)) == 1


def test_ordinary_sources_get_no_warning(tmp_path: Path) -> None:
    validator = validated(
        tmp_path, [native("a"), native("b", dataset_name="raw_custom"), native("a_other")]
    )

    assert not [r for r in validator.results if r.name.startswith("Dataset")]


def test_a_file_source_has_no_merge_staging_so_it_does_not_collide(tmp_path: Path) -> None:
    validator = validated(tmp_path, [local("a"), native("a_staging")])

    assert not collisions(validator)
