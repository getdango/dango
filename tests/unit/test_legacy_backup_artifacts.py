"""tests/unit/test_legacy_backup_artifacts.py

Verify legacy backup discovery never opens or removes artifact contents.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.security.legacy_backup_artifacts import find_legacy_backup_artifacts


@pytest.mark.unit
def test_finds_only_known_local_legacy_artifact_locations(tmp_path: Path) -> None:
    backup_dir = tmp_path / ".dango" / "backups"
    backup_dir.mkdir(parents=True)
    downloaded = tmp_path / "dango-backup-example.tar.gz"
    downloaded.write_text("not inspected", encoding="utf-8")
    (tmp_path / "unrelated.tar.gz").write_text("ignored", encoding="utf-8")

    assert find_legacy_backup_artifacts(tmp_path) == [backup_dir, downloaded]


@pytest.mark.unit
def test_empty_project_has_no_legacy_artifact_report(tmp_path: Path) -> None:
    assert find_legacy_backup_artifacts(tmp_path) == []


@pytest.mark.unit
def test_discovery_does_not_open_or_delete_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "dango-backup-old.tar.gz"
    artifact.write_text("opaque", encoding="utf-8")

    with patch.object(Path, "open", side_effect=AssertionError("must not read artifacts")):
        assert find_legacy_backup_artifacts(tmp_path) == [artifact]
    assert artifact.exists()
