"""tests/factories/scaffold_project.py

Projects built by the real `dango init --skip-wizard`, and a minimal scaffold for fake-project tests.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
from pathlib import Path

import pytest
import yaml

from dango.cli.init import ProjectInitializer


def free_port() -> int:
    """A currently free loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real init (no wizard, no driver download, no dbt docs) in a new directory."""
    home = tmp_path_factory.mktemp("home")
    root = tmp_path_factory.mktemp("fresh") / "proj"
    patcher = pytest.MonkeyPatch()
    patcher.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    patcher.setenv("HOME", str(home))
    patcher.setenv("USERPROFILE", str(home))
    patcher.setenv("DANGO_TELEMETRY", "0")
    patcher.setattr("dango.utils.driver.reuse_verified_driver", lambda plugins_dir: True)
    patcher.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: True)
    try:
        ProjectInitializer(root).initialize(skip_wizard=True)
    finally:
        patcher.undo()
    return root


def working_copy(fresh: Path, tmp_path: Path) -> Path:
    """A private copy of `fresh` with free ports and one CSV source in `inputs/`."""
    root = tmp_path / "proj"
    shutil.copytree(fresh, root)
    project_yml = root / ".dango" / "project.yml"
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8"))
    data["platform"] = {
        k: free_port() for k in ("port", "metabase_port", "dbt_docs_port", "marimo_port")
    }
    project_yml.write_text(yaml.safe_dump(data), encoding="utf-8")
    (root / ".dango" / "sources.yml").write_text(
        "version: '1.0'\nsources:\n  - name: demo\n    type: csv\n    csv:\n      directory: inputs\n",
        encoding="utf-8",
    )
    (root / "inputs").mkdir()
    (root / "inputs" / "a.csv").write_text("id,v\n1,2\n3,4\n", encoding="utf-8")
    return root


def tree(root: Path) -> dict[str, str]:
    """Relative path -> content hash (or 'dir'), ignoring the CLI's own log files."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".dango/logs"):
            continue
        found[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def make_scaffold(root: Path) -> None:
    """The minimal files `start`, `sync` and `run` look for, for tests with a bare fake project."""
    for name in ("Dockerfile.metabase", "entrypoint.sh", "dbt/dbt_project.yml", "dbt/profiles.yml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    for name in ("data", ".dlt"):
        (root / name).mkdir(exist_ok=True)
