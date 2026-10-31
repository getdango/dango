"""tests/integration/test_metabase_collection_repair_docker.py

Real pinned Metabase: idempotent collection import and empty-duplicate repair (K-34 rehearsal).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
import yaml

from dango.platform.common.startup import import_dashboards
from dango.visualization.dashboard_manager import DashboardManager

_TEMPLATE = Path(__file__).parents[2] / "dango" / "templates" / "Dockerfile.metabase"
PASSWORD = "Rehearsal-pw-98765"  # throwaway credential for a disposable container
EMAIL = "rehearsal@example.com"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pinned_image() -> str:
    match = re.search(r"MB_VERSION=(v[\d.]+)", _TEMPLATE.read_text())
    assert match, "Dockerfile.metabase no longer pins MB_VERSION"
    return f"metabase/metabase:{match.group(1)}"


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"docker {' '.join(args)}: {result.stderr.strip()}"
    return result.stdout.strip()


class Rehearsal:
    """Handle on one disposable Metabase container, its volume and its API session."""

    def __init__(self, base_url: str, session: str) -> None:
        self.base_url = base_url
        self.headers = {"X-Metabase-Session": session}

    def get(self, path: str) -> Any:
        resp = requests.get(f"{self.base_url}{path}", headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        resp = requests.post(f"{self.base_url}{path}", json=body, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def add_collection(self, name: str, parent: int | None = None) -> int:
        return int(self.post("/api/collection", {"name": name, "parent_id": parent})["id"])

    def active(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.get("/api/collection") if c.get("name") == name]

    def counts(self) -> tuple[int, int]:
        return len(self.get("/api/dashboard")), len(self.get("/api/card"))


@pytest.fixture(scope="module")
def metabase() -> Iterator[Rehearsal]:
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
    assert shutil.which("docker") is not None, "DANGO_RUN_DOCKER_TESTS=1 but docker is missing"
    suffix = uuid.uuid4().hex[:8]
    name, volume, port = f"dango-c7b-{suffix}", f"dango-c7b-{suffix}", _free_port()
    _docker("volume", "create", volume)
    try:
        _docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:3000",
            "-v",
            f"{volume}:/metabase-data",
            "-e",
            "MB_DB_FILE=/metabase-data/mb.db",
            _pinned_image(),
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                if requests.get(f"{base}/api/health", timeout=3).json().get("status") == "ok":
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(3)
        else:
            raise AssertionError("Metabase did not become healthy")
        token = requests.get(f"{base}/api/session/properties", timeout=30).json()["setup-token"]
        requests.post(
            f"{base}/api/setup",
            json={
                "token": token,
                "user": {"first_name": "R", "last_name": "H", "email": EMAIL, "password": PASSWORD},
                "prefs": {"site_name": "rehearsal", "allow_tracking": False},
            },
            timeout=60,
        ).raise_for_status()
        sid = requests.post(
            f"{base}/api/session", json={"username": EMAIL, "password": PASSWORD}, timeout=30
        ).json()["id"]
        yield Rehearsal(base, sid)
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=120)
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, timeout=120)


def _startup_import(project: Path, mb: Rehearsal, url: str | None = None) -> dict[str, Any] | None:
    """Run the normal start/serve import entry point against the disposable instance."""
    with (
        patch.object(
            DashboardManager,
            "_load_credentials",
            lambda self: setattr(self, "session_token", mb.headers["X-Metabase-Session"]),
        ),
        patch(
            "dango.visualization.dashboard_manager.resolve_metabase_url",
            return_value=url or mb.base_url,
        ),
    ):
        return import_dashboards(project)


def _export(project: Path, rows: list[tuple[int, str, int | None]]) -> None:
    mdir = project / "metabase"
    mdir.mkdir(parents=True, exist_ok=True)
    data = [{"id": i, "name": n, "parent_id": p} for i, n, p in rows]
    (mdir / "collections.yml").write_text(yaml.safe_dump({"collections": data}))


def _collection_mutations(calls: list[Any]) -> list[Any]:
    return [c for c in calls if "/api/collection" in str(c.args[0])]


def _second_start_is_noop(project: Path, mb: Rehearsal) -> None:
    with (
        patch("requests.post", wraps=requests.post) as posts,
        patch("requests.put", wraps=requests.put) as puts,
    ):
        result = _startup_import(project, mb)
    assert result is not None
    assert _collection_mutations(posts.call_args_list) == []
    assert _collection_mutations(puts.call_args_list) == []
    assert result["collection_repair"]["archived"] == 0
    assert result["collection_repair"]["created"] == 0


def test_first_import_then_restart_creates_hierarchy_once(metabase, tmp_path):
    _export(tmp_path, [(1, "RH Shared", None), (2, "Sub", 1), (3, "Other", None)])
    first = _startup_import(tmp_path, metabase)
    assert first is not None and first["collection_repair"]["created"] == 3
    assert len(metabase.active("RH Shared")) == 1
    _second_start_is_noop(tmp_path, metabase)
    assert len(metabase.active("RH Shared")) == 1
    assert len(metabase.active("Sub")) == 1


def test_beta_shape_one_canonical_plus_27_empty_siblings_is_repaired(metabase, tmp_path):
    canonical = metabase.add_collection("RH Historic")
    metabase.post("/api/dashboard", {"name": "Keep me", "collection_id": canonical})
    duplicates = [metabase.add_collection("RH Historic") for _ in range(27)]
    before = metabase.counts()
    _export(tmp_path, [(canonical, "RH Historic", None)])

    result = _startup_import(tmp_path, metabase)

    assert result is not None
    assert [c["id"] for c in metabase.active("RH Historic")] == [canonical]
    assert sorted(result["collection_repair"]["archived_ids"]) == sorted(duplicates)
    assert metabase.counts() == before
    assert (tmp_path / ".dango/state/metabase_collection_repair.json").is_file()
    _second_start_is_noop(tmp_path, metabase)


def test_populated_duplicate_wins_and_only_empties_are_archived(metabase, tmp_path):
    empties = [metabase.add_collection("RH Team") for _ in range(3)]
    populated = metabase.add_collection("RH Team")
    metabase.post("/api/dashboard", {"name": "Team board", "collection_id": populated})
    _export(tmp_path, [(empties[0], "RH Team", None)])

    result = _startup_import(tmp_path, metabase)

    assert result is not None
    assert [c["id"] for c in metabase.active("RH Team")] == [populated]
    assert sorted(result["collection_repair"]["archived_ids"]) == sorted(empties)


def test_unreachable_metabase_on_first_start_changes_nothing_then_repairs(metabase, tmp_path):
    for _ in range(3):
        metabase.add_collection("RH Retry")
    _export(tmp_path, [(1, "RH Retry", None)])
    first = _startup_import(tmp_path, metabase, url=f"http://127.0.0.1:{_free_port()}")
    assert first is not None and first["errors"]
    assert len(metabase.active("RH Retry")) == 3
    second = _startup_import(tmp_path, metabase)
    assert second is not None and second["collection_repair"]["archived"] == 2
    assert len(metabase.active("RH Retry")) == 1
