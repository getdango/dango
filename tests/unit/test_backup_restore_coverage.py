"""tests/unit/test_backup_restore_coverage.py

All three restores bring back the new backup contents, still clear stale WALs, accept old archives, and never turn auth off on a server.
"""

from __future__ import annotations

import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from tests.factories.backup_shell import (
    ShellSSH,
    duckdb_state,
    make_duckdb,
    make_duckdb_with_leftover_wal,
    make_sqlite,
    run_spaces_restore,
)

_B = "dango.platform.cloud.backup"
_TOP = "backup-20260101-000000"


def _new_layout(content: Path) -> None:
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    (content / "data" / "uploads").mkdir(parents=True)
    (content / "data" / "uploads" / "x.csv").write_text("a\n1\n")
    (content / "notebooks").mkdir()
    (content / "notebooks" / "n.py").write_text("import marimo\n")
    (content / "scripts").mkdir()
    (content / "scripts" / "s.py").write_text("print('archive')\n")
    (content / ".dango").mkdir()
    (content / ".dango" / "schedules.yml").write_text("schedules: []\n")
    make_sqlite(content / ".dango" / "auth.db", "archive")
    make_sqlite(content / ".dango" / "scheduler.db", "archive")
    (content / "manifest.json").write_text("{}")


def _old_layout(content: Path) -> None:
    """An archive written from the pre-change lists (warehouse file, data/seeds, .dlt/pipelines)."""
    make_duckdb(content / "data" / "warehouse.duckdb", "archive")
    (content / "data" / "seeds").mkdir(parents=True)
    (content / "data" / "seeds" / "s.csv").write_text("k\n1\n")
    (content / ".dlt" / "pipelines" / "src").mkdir(parents=True)
    (content / ".dlt" / "pipelines" / "src" / "state.json").write_text("{}")
    (content / ".dango").mkdir()
    make_sqlite(content / ".dango" / "auth.db", "archive")
    (content / ".dango" / "project.yml").write_text("project: {}\n")
    (content / "dbt" / "models").mkdir(parents=True)
    (content / "dbt" / "models" / "m.sql").write_text("select 'archive'\n")
    (content / "manifest.json").write_text("{}")


def _tar(content: Path, archive: Path) -> Path:
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=_TOP)
    return archive


# ---------------------------------------------------------------------------
# One runner per restore implementation; each returns the warnings it reported
# ---------------------------------------------------------------------------


def _server_restore(tmp_path: Path, project: Path, build: Callable[[Path], None]) -> list[str]:
    from dango.platform.cloud.backup import restore_from_archive

    content = tmp_path / "content"
    build(content)
    archive = _tar(content, tmp_path / f"{_TOP}.tar.gz")
    (tmp_path / "staging-root").mkdir(exist_ok=True)
    with (
        patch(f"{_B}.PROJECT_DIR", str(project)),
        patch(f"{_B}.RESTORE_STAGING_ROOT", str(tmp_path / "staging-root")),
        patch(f"{_B}.create_backup", return_value=MagicMock(archive_path="safety.tar.gz")),
        patch(f"{_B}.stop_services"),
        patch(f"{_B}.start_services"),
        patch(f"{_B}.verify_health", return_value=True),
        patch(f"{_B}._get_metabase_volume_path", return_value=None),
    ):
        result = restore_from_archive(ShellSSH(tmp_path / "shim"), str(archive))
    return list(result.warnings)


def _spaces_restore(tmp_path: Path, project: Path, build: Callable[[Path], None]) -> list[str]:
    build(tmp_path / _TOP)
    logger = MagicMock()
    with patch("dango.platform.cloud.scheduled_backup._logger", logger):
        run_spaces_restore(tmp_path, project, _TOP)
    return [str(c) for c in logger.warning.call_args_list]


def _local_restore(
    tmp_path: Path, project: Path, build: Callable[[Path], None], *, cloud: bool = False
) -> list[str]:
    from dango.cli.commands.local_backup import backup_group

    content = tmp_path / "content"
    build(content)
    archive = _tar(content, tmp_path / f"{_TOP}.tar.gz")
    env = {"DANGO_CLOUD_MODE": "true"} if cloud else {}
    with (
        patch("dango.cli.commands.local_backup.require_project_context", return_value=project),
        patch.dict("os.environ", env),
    ):
        result = CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )
    assert result.exit_code == 0, result.output
    return [result.output]


RESTORES: dict[str, Callable[..., list[str]]] = {
    "server": _server_restore,
    "spaces": _spaces_restore,
    "local": _local_restore,
}


@pytest.fixture(params=sorted(RESTORES))
def restore(request: pytest.FixtureRequest) -> Callable[..., list[str]]:
    return RESTORES[str(request.param)]


def _live_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    make_duckdb_with_leftover_wal(project / "data" / "warehouse.duckdb")
    (project / ".dango").mkdir(exist_ok=True)
    return project


@pytest.mark.unit
def test_restore_brings_back_new_backup_contents(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    restore(tmp_path, project, _new_layout)
    assert (project / "data" / "uploads" / "x.csv").read_text() == "a\n1\n"
    assert (project / "notebooks" / "n.py").exists()
    assert (project / "scripts" / "s.py").read_text() == "print('archive')\n"
    assert (project / ".dango" / "schedules.yml").exists()
    assert (project / ".dango" / "scheduler.db").exists()


@pytest.mark.unit
def test_restore_still_removes_warehouse_wal_after_list_change(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    live_wal = project / "data" / "warehouse.duckdb.wal"
    assert live_wal.exists()
    restore(tmp_path, project, _new_layout)
    assert not live_wal.exists(), "stale warehouse WAL left next to the restored warehouse"
    tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert tables == {"t"}, f"live WAL replayed into restored warehouse: {tables}"
    assert values == ["archive"]


@pytest.mark.unit
def test_restore_old_archive_layout_still_works(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    restore(tmp_path, project, _old_layout)
    _tables, values = duckdb_state(project / "data" / "warehouse.duckdb")
    assert values == ["archive"]
    assert (project / "data" / "seeds" / "s.csv").exists()
    assert (project / "dbt" / "models" / "m.sql").read_text() == "select 'archive'\n"
    assert not (project / ".dlt" / "pipelines").exists(), "dlt load copies restored"


def _with_auth(enabled: str) -> Callable[[Path], None]:
    def build(content: Path) -> None:
        _new_layout(content)
        (content / ".dango" / "auth.yml").write_text(f"enabled: {enabled}\n")

    return build


def _server_like(restore: Callable[..., list[str]]) -> Callable[..., list[str]]:
    if restore is _local_restore:
        return lambda *a: _local_restore(*a, cloud=True)
    return restore


@pytest.mark.unit
def test_restore_never_disables_auth_on_server(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    live_auth = project / ".dango" / "auth.yml"
    live_auth.write_text("enabled: true\n")
    warnings = _server_like(restore)(tmp_path, project, _with_auth("false"))
    assert live_auth.read_text() == "enabled: true\n"
    assert any("auth.yml" in w for w in warnings), warnings


@pytest.mark.unit
def test_restore_on_server_keeps_live_auth_when_archive_has_none(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    live_auth = project / ".dango" / "auth.yml"
    live_auth.write_text("enabled: true\n")
    _server_like(restore)(tmp_path, project, _new_layout)
    assert live_auth.read_text() == "enabled: true\n"


@pytest.mark.unit
def test_restore_on_server_restores_auth_yml_that_keeps_auth_on(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    project = _live_project(tmp_path)
    (project / ".dango" / "auth.yml").write_text("enabled: true\nold: 1\n")
    _server_like(restore)(tmp_path, project, _with_auth("true"))
    assert (project / ".dango" / "auth.yml").read_text() == "enabled: true\n"


@pytest.mark.unit
def test_local_restore_off_server_restores_auth_yml_as_is(tmp_path: Path) -> None:
    project = _live_project(tmp_path)
    (project / ".dango" / "auth.yml").write_text("enabled: true\n")
    _local_restore(tmp_path, project, _with_auth("false"))
    assert (project / ".dango" / "auth.yml").read_text() == "enabled: false\n"


def _warehouse_only(content: Path) -> None:
    (content / ".dango").mkdir(parents=True)
    (content / ".dango" / "schedules.yml").write_text("schedules: []\n")
    (content / "manifest.json").write_text("{}")


def _live_pipeline_state(project: Path) -> Path:
    state = project / ".dlt" / "pipelines" / "src" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"live": true}')
    return state


@pytest.mark.unit
@pytest.mark.parametrize("which", ["server", "spaces"])
def test_restore_moves_live_pipeline_state_aside(which: str, tmp_path: Path) -> None:
    restore = RESTORES[which]
    project = _live_project(tmp_path)
    _live_pipeline_state(project)
    restore(tmp_path, project, _new_layout)
    assert not (project / ".dlt" / "pipelines").exists(), "live pipeline state left in place"
    kept = list((project / ".dlt").glob("pipelines.pre-restore-*"))
    assert len(kept) == 1, kept
    assert (kept[0] / "src" / "state.json").read_text() == '{"live": true}'


@pytest.mark.unit
@pytest.mark.parametrize("which", ["server", "spaces"])
def test_restore_without_warehouse_keeps_live_pipeline_state(which: str, tmp_path: Path) -> None:
    restore = RESTORES[which]
    project = _live_project(tmp_path)
    state = _live_pipeline_state(project)
    restore(tmp_path, project, _warehouse_only)
    assert state.read_text() == '{"live": true}'
    assert not list((project / ".dlt").glob("pipelines.pre-restore-*"))


@pytest.mark.unit
def test_restore_refuses_oversized_auth_yml(
    restore: Callable[..., list[str]], tmp_path: Path
) -> None:
    def build(content: Path) -> None:
        _new_layout(content)
        (content / ".dango" / "auth.yml").write_text("enabled: true\n" + "# pad\n" * 20000)

    project = _live_project(tmp_path)
    live_auth = project / ".dango" / "auth.yml"
    live_auth.write_text("enabled: true\n")
    warnings = _server_like(restore)(tmp_path, project, build)
    assert live_auth.read_text() == "enabled: true\n"
    assert any("auth.yml" in w for w in warnings), warnings


@pytest.mark.unit
@pytest.mark.parametrize("which", ["server", "spaces"])
def test_two_restores_in_one_second_do_not_nest_pipeline_state(which: str, tmp_path: Path) -> None:
    restore = RESTORES[which]
    project = _live_project(tmp_path)
    _live_pipeline_state(project)
    with patch("time.strftime", return_value="20260101-000000"):
        restore(tmp_path, project, _new_layout)
        _live_pipeline_state(project)
        shutil.rmtree(tmp_path / "content", ignore_errors=True)
        shutil.rmtree(tmp_path / _TOP, ignore_errors=True)
        restore(tmp_path, project, _new_layout)
    kept = sorted(p.name for p in (project / ".dlt").iterdir())
    assert len(kept) == 2 and all(n.startswith("pipelines.pre-restore-") for n in kept), kept
    for path in (project / ".dlt").iterdir():
        assert (path / "src" / "state.json").is_file(), f"nested: {path}"
