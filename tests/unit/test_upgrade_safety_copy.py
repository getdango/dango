"""tests/unit/test_upgrade_safety_copy.py

`dango upgrade` copies .dango databases and config aside before migrating, even with --yes, and aborts if it cannot.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli
from tests.factories.backup_shell import make_sqlite, make_sqlite_with_leftover_wal


def _project(tmp_path: Path) -> Path:
    dango = tmp_path / ".dango"
    dango.mkdir(parents=True)
    (dango / "project.yml").write_text("project:\n  name: test\n")
    (dango / "schedules.yml").write_text("schedules: []\n")
    make_sqlite_with_leftover_wal(dango / "auth.db")
    make_sqlite(dango / "scheduler.db", "jobs")
    return tmp_path


def _upgrade(project: Path, on_migrate: Any = None) -> tuple[Any, MagicMock, MagicMock]:
    subprocess_mock = MagicMock()
    subprocess_mock.run.return_value = MagicMock(returncode=0, stderr="")
    subprocess_mock.CalledProcessError = subprocess.CalledProcessError
    subprocess_mock.TimeoutExpired = subprocess.TimeoutExpired
    migrate = MagicMock(side_effect=on_migrate or (lambda _root: {}))
    with (
        patch("dango.cli.utils.find_project_root", return_value=project),
        patch("dango.cli.commands.upgrade.subprocess", subprocess_mock),
        patch("dango.migrations.apply_all_pending", migrate),
        patch("dango.__version__", "0.1.0"),
    ):
        result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])
    return result, subprocess_mock, migrate


def _copies(project: Path) -> list[Path]:
    return sorted((project / ".dango" / "backups").glob("pre-upgrade-*"))


def _rows(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return sorted(r[0] for r in conn.execute("SELECT n FROM u"))
    finally:
        conn.close()


@pytest.mark.unit
def test_upgrade_makes_safety_copy_even_with_yes(tmp_path: Path) -> None:
    project = _project(tmp_path)
    seen: list[list[Path]] = []

    result, _sp, migrate = _upgrade(
        project, on_migrate=lambda _r: seen.append(_copies(project)) or {}
    )

    assert result.exit_code == 0, result.output
    assert migrate.called
    assert seen and len(seen[0]) == 1, "no safety copy existed when migrations ran"
    (copy,) = _copies(project)
    assert str(copy.name) in result.output
    assert (copy / "project.yml").exists() and (copy / "schedules.yml").exists()
    assert (copy / "scheduler.db").exists()
    # auth.db's committed row that was not yet checkpointed is in the copy.
    assert _rows(copy / "auth.db") == ["live", "old"]


@pytest.mark.unit
def test_upgrade_aborts_if_safety_copy_fails(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".dango" / "backups").write_text("not a directory")

    result, subprocess_mock, migrate = _upgrade(project)

    assert result.exit_code != 0
    assert "safety copy" in result.output.lower()
    assert not migrate.called, "migrations ran without a safety copy"
    pip_installs = [
        c for c in subprocess_mock.run.call_args_list if "install" in [str(a) for a in c.args[0]]
    ]
    assert not pip_installs, "package was upgraded without a safety copy"


@pytest.mark.unit
def test_upgrade_keeps_newest_three_safety_copies(tmp_path: Path) -> None:
    project = _project(tmp_path)
    backups = project / ".dango" / "backups"
    backups.mkdir()
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        (backups / f"pre-upgrade-{stamp}").mkdir()
    (backups / "pre-restore-keep.tar.gz").write_bytes(b"x")

    result, _sp, _m = _upgrade(project)

    assert result.exit_code == 0, result.output
    names = [p.name for p in _copies(project)]
    assert len(names) == 3
    assert "pre-upgrade-20200101T000000Z" not in names
    assert (backups / "pre-restore-keep.tar.gz").exists()


@pytest.mark.unit
def test_upgrade_safety_copy_never_holds_the_metabase_password(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".dango" / "metabase.yml").write_text(
        "url: http://localhost:3000\nadmin:\n  email: a@example.com\n  password: s3cret-pw\n"
    )

    result, _sp, _m = _upgrade(project)

    assert result.exit_code == 0, result.output
    (copy,) = _copies(project)
    text = (copy / "metabase.yml").read_text()
    assert "s3cret-pw" not in text and "a@example.com" in text
    # The copy made by this upgrade is not reported as a possibly legacy artifact.
    assert "may predate" not in result.output


@pytest.mark.unit
def test_failed_safety_copy_leaves_no_partial_copy(tmp_path: Path) -> None:
    project = _project(tmp_path)

    def fail_on_yml(src: Any, dst: Any, **kwargs: Any) -> Any:
        if str(src).endswith(".yml"):
            raise OSError("disk full")
        return shutil.copy2(src, dst, **kwargs)

    with patch("dango.cli.commands.upgrade.shutil.copy2", side_effect=fail_on_yml):
        result, _sp, migrate = _upgrade(project)

    assert result.exit_code != 0
    assert not migrate.called
    assert _copies(project) == [], "partial safety copy left behind"
