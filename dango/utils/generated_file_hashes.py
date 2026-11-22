"""dango/utils/generated_file_hashes.py

SHA-256 record of the bytes Dango last wrote to a generated file the user may edit. A later
automatic rewrite is allowed only while the file still matches its record, so a file the user
changed (or a project with no record) is never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from dango.logging import get_logger

logger = get_logger(__name__)

SIDECAR_NAME = "staging_yaml_hashes.json"


def _sidecar(project_root: Path) -> Path:
    return project_root / ".dango" / "state" / SIDECAR_NAME


def _key(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def _read_records(project_root: Path) -> dict[str, str]:
    """The recorded hashes; empty when the record is missing or unreadable."""
    try:
        data = json.loads(_sidecar(project_root).read_text(encoding="utf-8"))
        files = data["files"]
        return {str(k): str(v) for k, v in files.items()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_unmodified(project_root: Path, path: Path) -> bool:
    """True when ``path`` exists and its bytes still match what Dango recorded for it."""
    try:
        recorded = _read_records(project_root).get(_key(project_root, path))
        return recorded is not None and recorded == _digest(path)
    except (OSError, ValueError):
        return False


def record_current(project_root: Path, path: Path) -> bool:
    """Record the hash of the file as it is on disk now. Never raises; False if not recorded."""
    tmp_name = None
    try:
        records = _read_records(project_root)
        records[_key(project_root, path)] = _digest(path)
        target = _sidecar(project_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "files": records}, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=target.parent, suffix=".tmp", delete=False
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(payload)
        os.replace(tmp_name, target)
        return True
    except (OSError, ValueError):
        logger.warning("generated_file_hash_not_recorded", file=str(path), exc_info=True)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
