"""tests/unit/test_mcp_tool_guards.py

One test per guard the MCP tools rely on: remote_push confirm and dry-run default, source host changes, model and query file access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
import yaml

from dango.cli.commands import mcp_models, mcp_remote, mcp_server, mcp_sources
from dango.config.helpers import load_config, save_config

CANARY = "dango-c5k-canary-4b1d"
LEAK_SQL = "select content from read_text('{path}')"


# ---------------------------------------------------------------------------
# Remote tools (read dango/cli/commands/mcp_remote.py: remote_push is the only
# tool whose destructive action sits behind dry_run=True / confirm=False defaults)
# ---------------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Deployed project whose SSH connection is a stub; yields the ``_ssh`` mock."""
    cfg = SimpleNamespace(
        droplet_ip="203.0.113.7", ssh_key_path=".dango/cloud_key", deploy_branch="main"
    )
    monkeypatch.setattr(mcp_remote, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_remote, "_cloud", lambda _root: cfg)
    connect = MagicMock()
    monkeypatch.setattr(mcp_remote, "_ssh", connect)
    return connect


def _clean_git() -> Any:
    from dango.utils.git_info import GitInfo

    return GitInfo(commit_sha="abc", branch="main", is_clean=True, is_git_repo=True)


def _push_result(dry_run: bool) -> Any:
    from dango.platform.cloud.deployer import DeployResult
    from dango.platform.cloud.file_sync import SyncResult

    return DeployResult(sync_result=SyncResult(dry_run=dry_run), dry_run=dry_run)


@pytest.mark.unit
def test_remote_push_refuses_real_push_without_confirm(remote: Any) -> None:
    with patch("dango.platform.cloud.deployer.push_deploy") as push:
        out = mcp_remote.remote_push(dry_run=False)

    assert "explicitly approves" in out["error"]
    push.assert_not_called()
    remote.assert_not_called()


@pytest.mark.unit
def test_remote_push_defaults_to_dry_run(remote: Any) -> None:
    with (
        patch("dango.utils.git_info.collect_git_info", return_value=_clean_git()),
        patch("dango.platform.cloud.deployer.push_deploy", return_value=_push_result(True)) as push,
    ):
        mcp_remote.remote_push()

    assert push.call_args.kwargs["dry_run"] is True
    assert push.call_args.kwargs["force"] is False


# ---------------------------------------------------------------------------
# Source host changes (C1d)
# ---------------------------------------------------------------------------


@pytest.fixture
def source_project(tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    return root


def _source_names(root: Path) -> list[str]:
    return [s.name for s in load_config(root).sources.sources]


@pytest.mark.unit
def test_create_source_rejects_hostile_host(source_project: Path) -> None:
    out = mcp_sources.create_source("freshdesk", "foo", {"domain": "evil.example#"})

    assert out.get("error")
    assert _source_names(source_project) == []


@pytest.mark.unit
def test_update_source_rejects_host_change(source_project: Path) -> None:
    created = mcp_sources.create_source("freshdesk", "foo", {"domain": "acme"})
    assert created.get("status") == "created", created
    before = (source_project / ".dango" / "sources.yml").read_text()

    out = mcp_sources.update_source("foo", {"domain": "evil"})

    assert out.get("error")
    assert (source_project / ".dango" / "sources.yml").read_text() == before


# ---------------------------------------------------------------------------
# Model SQL file access (C1c)
# ---------------------------------------------------------------------------


@pytest.fixture
def model_project(tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    save_config(sample_config, tmp_path)
    stg = tmp_path / "dbt" / "models" / "staging"
    stg.mkdir(parents=True)
    (stg / "stg_x__orders.sql").write_text("select 1 as order_id\n")
    monkeypatch.setattr(mcp_models, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_models, "_git_warnings", lambda project_root: [])
    monkeypatch.setattr("dango.transformation.model_service.parse_project", lambda root: (True, ""))
    return tmp_path


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.unit
def test_create_model_rejects_file_access(model_project: Path, tmp_path: Path) -> None:
    before = _files(model_project)

    out = mcp_models.create_model(
        "leak", "marts", sql=LEAK_SQL.format(path=tmp_path / "secret.txt")
    )

    assert out.get("errors") or out.get("error")
    assert _files(model_project) == before


@pytest.mark.unit
def test_update_model_rejects_file_access(model_project: Path, tmp_path: Path) -> None:
    before = _files(model_project)

    out = mcp_models.update_model(
        "stg_x__orders", sql=LEAK_SQL.format(path=tmp_path / "secret.txt")
    )

    assert out.get("errors") or out.get("error")
    assert _files(model_project) == before


# ---------------------------------------------------------------------------
# Query file access (C1a)
# ---------------------------------------------------------------------------


@pytest.fixture
def query_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "qproj"
    (project / "data").mkdir(parents=True)
    (project / ".dango").mkdir()
    (project / ".dango" / "project.yml").write_text(
        yaml.safe_dump({"project": {"name": "Q", "created_by": "t@example.com", "purpose": "t"}})
    )
    (project / ".dango" / "sources.yml").write_text(yaml.safe_dump({"sources": []}))
    conn = duckdb.connect(str(project / "data" / "warehouse.duckdb"))
    conn.execute("create table t as select 1 as a")
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_project_root", lambda: project)
    return project


@pytest.mark.unit
def test_query_refuses_file_access(query_project: Path, tmp_path: Path) -> None:
    canary = tmp_path / "canary.txt"
    canary.write_text(CANARY)

    out = asyncio.run(mcp_server.query(LEAK_SQL.format(path=canary)))

    assert "error" in out
    assert CANARY not in str(out)
    assert asyncio.run(mcp_server.query("select a from t"))["rows"] == [[1]]
