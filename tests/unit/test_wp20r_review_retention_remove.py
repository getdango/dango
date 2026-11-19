"""tests/unit/test_wp20r_review_retention_remove.py

Safety-copy retention only touches generated names and honours keep=1; a failed source remove names the .env keys left and is not reported as removed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import mcp_sources
from dango.cli.commands import upgrade as upgrade_module
from dango.cli.commands.upgrade import _pre_upgrade_safety_copy
from dango.config.helpers import save_config
from dango.config.models import DataSource


def _stamped(backups: Path, *stamps: str) -> None:
    for stamp in stamps:
        (backups / f"pre-upgrade-{stamp}").mkdir()


@pytest.mark.unit
def test_a_hand_made_pre_upgrade_directory_is_neither_counted_nor_pruned(tmp_path: Path) -> None:
    backups = tmp_path / ".dango" / "backups"
    backups.mkdir(parents=True)
    _stamped(backups, "20200101T000000Z", "20200102T000000Z", "20200103T000000Z")
    (backups / "pre-upgrade-notes").mkdir()
    (backups / "pre-upgrade-0old").mkdir()
    dest = _pre_upgrade_safety_copy(tmp_path)
    assert (backups / "pre-upgrade-notes").is_dir()
    assert (backups / "pre-upgrade-0old").is_dir()
    assert not (backups / "pre-upgrade-20200101T000000Z").exists()
    assert (backups / "pre-upgrade-20200102T000000Z").is_dir()
    assert (backups / "pre-upgrade-20200103T000000Z").is_dir()
    assert dest.is_dir()


@pytest.mark.unit
def test_keeping_one_copy_keeps_exactly_one(tmp_path: Path) -> None:
    backups = tmp_path / ".dango" / "backups"
    backups.mkdir(parents=True)
    _stamped(backups, "20200101T000000Z", "20200102T000000Z")
    with patch.object(upgrade_module, "_SAFETY_COPIES_KEPT", 1):
        dest = _pre_upgrade_safety_copy(tmp_path)
    assert sorted(p.name for p in backups.glob("pre-upgrade-*")) == [dest.name]


def _project(tmp_path: Path, sample_config) -> Path:
    sample_config.sources.sources = [
        DataSource(
            name="orders",
            type="local_files",
            local_files={"directory": "data/uploads/orders", "file_pattern": "*.csv"},
        )
    ]
    save_config(sample_config, tmp_path)
    staging = tmp_path / "dbt" / "models" / "staging"
    staging.mkdir(parents=True)
    for name in ("stg_orders__t.sql", "sources_orders.yml"):
        (staging / name).write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("ORDERS_KEY=v\nUNRELATED=w\n", encoding="utf-8")
    return tmp_path


def _stuck(name: str):
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == name:
            raise PermissionError("in use")
        real(self, missing_ok=missing_ok)

    return unlink


@pytest.mark.unit
def test_failed_source_remove_names_the_env_keys_it_left(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.main import cli

    project = _project(tmp_path, sample_config)
    monkeypatch.chdir(project)
    monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: None)
    with patch.object(Path, "unlink", _stuck("sources_orders.yml")):
        result = CliRunner().invoke(cli, ["source", "remove", "orders", "--yes"])
    out = " ".join(result.output.split())
    assert result.exit_code != 0
    assert "ORDERS_KEY" in out and ".env" in out
    assert "UNRELATED" not in out


@pytest.mark.unit
def test_mcp_remove_source_does_not_report_removed_when_a_file_stayed(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, sample_config)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: project)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    with patch.object(Path, "unlink", _stuck("sources_orders.yml")):
        result = mcp_sources.remove_source("orders")
    assert result["status"] != "removed"
    assert "error" in result and "sources_orders.yml" in result["error"]
