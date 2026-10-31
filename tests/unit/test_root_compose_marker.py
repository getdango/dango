"""tests/unit/test_root_compose_marker.py

The root-side wrapper picks the helper branch only for a root-owned, non-writable marker in
root-owned, non-writable directories; anything else (planted, writable, symlinked) runs the legacy command.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path

import pytest

from dango.platform.cloud import compose_helper as ch

pytestmark = pytest.mark.unit


def _run(tmp_path: Path, *, owner: str) -> str:
    """Run the generated wrapper against a temp tree; *owner* stands in for ``root``."""
    stub = tmp_path / "helper"
    stub.write_text("#!/bin/sh\necho HELPER\n")
    stub.chmod(0o755)
    cmd = ch.server_compose_cmd("stop", "metabase", legacy="echo LEGACY")
    cmd = cmd.replace(ch.HELPER_PATH, str(stub)).replace(ch.SRV_DIR, str(tmp_path / "srv"))
    cmd = cmd.replace("-user root", f"-user {owner}")
    done = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False)
    return done.stdout.strip()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    compose = tmp_path / "srv" / "compose"
    compose.mkdir(parents=True)
    for d in (tmp_path / "srv", compose):
        d.chmod(0o755)
    marker = compose / "active"
    marker.write_text("")
    marker.chmod(0o644)
    return tmp_path


_ME = getpass.getuser()
_OTHER = "nobody" if _ME != "nobody" else "daemon"


def test_trusted_marker_selects_the_helper(tree: Path) -> None:
    assert _run(tree, owner=_ME) == "HELPER"


def test_marker_owned_by_the_service_user_runs_legacy(tree: Path) -> None:
    assert _run(tree, owner=_OTHER) == "LEGACY"


def test_world_writable_marker_runs_legacy(tree: Path) -> None:
    (tree / "srv" / "compose" / "active").chmod(0o666)
    assert _run(tree, owner=_ME) == "LEGACY"


def test_group_writable_marker_runs_legacy(tree: Path) -> None:
    (tree / "srv" / "compose" / "active").chmod(0o664)
    assert _run(tree, owner=_ME) == "LEGACY"


def test_symlink_marker_runs_legacy(tree: Path) -> None:
    marker = tree / "srv" / "compose" / "active"
    real = tree / "real"
    real.write_text("")
    real.chmod(0o644)
    marker.unlink()
    os.symlink(real, marker)
    assert _run(tree, owner=_ME) == "LEGACY"


def test_missing_marker_runs_legacy(tree: Path) -> None:
    (tree / "srv" / "compose" / "active").unlink()
    assert _run(tree, owner=_ME) == "LEGACY"


@pytest.mark.parametrize("which", ["srv", "srv/compose"])
def test_writable_parent_directory_runs_legacy(tree: Path, which: str) -> None:
    (tree / which).chmod(0o775)
    assert _run(tree, owner=_ME) == "LEGACY"


def test_standalone_probe_matches_the_wrapper_test() -> None:
    assert ch.MARKER_TEST in ch.server_compose_cmd("stop", "metabase", legacy="true")
