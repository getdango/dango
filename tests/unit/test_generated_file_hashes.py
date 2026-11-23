"""tests/unit/test_generated_file_hashes.py

The hash sidecar is trusted only when it is version 1; any other version means no records.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dango.utils.generated_file_hashes import is_unmodified, record_current

REL = "dbt/models/staging/stg_shop.yml"


def _project(tmp_path: Path) -> Path:
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.write_bytes(b"version: 2\n")
    return path


def _sidecar(tmp_path: Path) -> Path:
    return tmp_path / ".dango" / "state" / "staging_yaml_hashes.json"


def _write_sidecar(tmp_path: Path, payload: dict) -> None:
    _sidecar(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _sidecar(tmp_path).write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.unit
def test_version_1_record_is_trusted(tmp_path: Path) -> None:
    path = _project(tmp_path)
    _write_sidecar(tmp_path, {"version": 1, "files": {REL: _sha(path)}})
    assert is_unmodified(tmp_path, path)


@pytest.mark.unit
@pytest.mark.parametrize("version", [None, 2, 0, "1", 1.0, True, [1]], ids=repr)
def test_any_other_version_means_no_records(tmp_path: Path, version: object) -> None:
    path = _project(tmp_path)
    payload: dict = {"files": {REL: _sha(path)}}
    if version is not None:
        payload["version"] = version
    _write_sidecar(tmp_path, payload)
    assert not is_unmodified(tmp_path, path)


@pytest.mark.unit
def test_recording_over_a_foreign_version_keeps_only_the_current_file(tmp_path: Path) -> None:
    path = _project(tmp_path)
    _write_sidecar(tmp_path, {"version": 2, "files": {"other.yml": "abc", REL: "stale"}})
    assert record_current(tmp_path, path)
    data = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert data == {"version": 1, "files": {REL: _sha(path)}}
    assert is_unmodified(tmp_path, path)


@pytest.mark.unit
def test_recording_keeps_other_files_of_a_version_1_sidecar(tmp_path: Path) -> None:
    path = _project(tmp_path)
    _write_sidecar(tmp_path, {"version": 1, "files": {"other.yml": "abc"}})
    assert record_current(tmp_path, path)
    data = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert data["files"] == {"other.yml": "abc", REL: _sha(path)}
