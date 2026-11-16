"""tests/unit/test_backup_coverage.py

Backups cover every user-writable project path (or exclude it on purpose), skip dlt load copies, WAL/temp files and links.
"""

from __future__ import annotations

import getpass
import resource
import shutil
import signal
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.factories.backup_create import (
    archive_text,
    run_scheduled_archive,
    run_server_archive,
)
from tests.factories.backup_shell import make_duckdb, make_sqlite, make_sqlite_with_leftover_wal

_BR = "dango.platform.cloud.backup_restore"

#: Paths a Dango project can contain, from a real ``dango init --skip-wizard`` tree plus
#: the paths Dango writes later (web UI, scheduler, syncs, uploads, deploys).  A new
#: user-writable path must be added here AND be backed up or listed in EXCLUDED_PATHS.
PROJECT_PATHS = (
    ".dango/auth.db",
    ".dango/auth.yml",
    ".dango/backups",
    ".dango/cloud.yml",
    ".dango/cloud_key",
    ".dango/credentials",
    ".dango/dango.db",
    ".dango/dbt_model_status.json",
    ".dango/dev",
    ".dango/history",
    ".dango/known_hosts",
    ".dango/logs/activity.jsonl",
    ".dango/logs/audit.jsonl",
    ".dango/logs/scripts",
    ".dango/metabase.yml",
    ".dango/metabase_telemetry_state",
    ".dango/monitors.yml",
    ".dango/pii-overrides.yml",
    ".dango/project.yml",
    ".dango/routing.json",
    ".dango/scheduler.db",
    ".dango/schedules.yml",
    ".dango/scripts.yml",
    ".dango/secrets",
    ".dango/snapshots",
    ".dango/sources.yml",
    ".dango/ssh",
    ".dango/state",
    ".dango/telemetry.json",
    ".dango/web.pid",
    ".dlt/config.toml",
    ".dlt/pipelines",
    ".dlt/secrets.toml",
    ".env",
    ".gitignore",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    "CLAUDE.md",
    "COMPATIBILITY.md",
    "Dockerfile.metabase",
    "README.md",
    "SCALABILITY.md",
    "custom_sources",
    "data/uploads",
    "data/warehouse",
    "data/warehouse.duckdb",
    "dbt/.user.yml",
    "dbt/analyses",
    "dbt/dbt_packages",
    "dbt/dbt_project.yml",
    "dbt/logs",
    "dbt/macros",
    "dbt/models",
    "dbt/packages.yml",
    "dbt/profiles.yml",
    "dbt/seeds",
    "dbt/snapshots",
    "dbt/target",
    "dbt/tests",
    "docker-compose.yml",
    "entrypoint.sh",
    "metabase",
    "metabase-plugins",
    "notebooks",
    "requirements.txt",
    "scripts",
)

#: Paths that hold user data on a server and must be in every backup.
MUST_BACK_UP = (
    "data/uploads",
    "data/warehouse.duckdb",
    "notebooks",
    "scripts",
    ".dango/auth.db",
    ".dango/auth.yml",
    ".dango/schedules.yml",
    ".dango/monitors.yml",
    ".dango/dango.db",
    ".dango/scheduler.db",
    ".dango/pii-overrides.yml",
    ".dango/scripts.yml",
)


def _covered(rel: str) -> bool:
    from dango.platform.cloud.backup import (
        BACKUP_DIRS,
        BACKUP_FILES,
        METABASE_CONFIG_FILE,
        SECRET_FILES,
    )

    return (
        rel == METABASE_CONFIG_FILE
        or rel in BACKUP_FILES
        or rel in SECRET_FILES
        or any(rel == d or rel.startswith(d + "/") for d in BACKUP_DIRS)
    )


@pytest.mark.unit
def test_every_user_writable_project_path_is_backed_up_or_excluded() -> None:
    from dango.platform.cloud.backup import EXCLUDED_PATHS

    neither = [p for p in PROJECT_PATHS if not _covered(p) and p not in EXCLUDED_PATHS]
    assert not neither, f"neither backed up nor excluded (with a reason): {neither}"
    both = [p for p in EXCLUDED_PATHS if _covered(p)]
    assert not both, f"excluded but also backed up: {both}"
    assert all(reason.strip() for reason in EXCLUDED_PATHS.values())
    missing = [p for p in MUST_BACK_UP if not _covered(p)]
    assert not missing, f"user data not backed up: {missing}"


@pytest.mark.unit
def test_warehouse_is_backed_up_through_the_data_directory() -> None:
    from dango.platform.cloud.backup import BACKUP_DIRS, BACKUP_FILES, DATABASE_FILES

    assert "data" in BACKUP_DIRS
    assert "data/warehouse.duckdb" not in BACKUP_FILES
    assert "data/seeds" not in BACKUP_DIRS
    assert set(DATABASE_FILES) == {
        "data/warehouse.duckdb",
        ".dango/auth.db",
        ".dango/dango.db",
        ".dango/scheduler.db",
    }


# ---------------------------------------------------------------------------
# What the two archive creators actually write
# ---------------------------------------------------------------------------


def _project(root: Path) -> Path:
    """A server project tree with user data in every newly covered place."""
    make_duckdb(root / "data" / "warehouse.duckdb", "live")
    (root / "data" / "uploads").mkdir(parents=True)
    (root / "data" / "uploads" / "x.csv").write_text("a,b\n1,2\n")
    (root / "data" / "seeds").mkdir()
    (root / "data" / "seeds" / "s.csv").write_text("k\n1\n")
    (root / "notebooks").mkdir()
    (root / "notebooks" / "n.py").write_text("import marimo\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "s.py").write_text("print('hi')\n")
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "models" / "m.sql").write_text("select 1\n")
    (root / "custom_sources").mkdir()
    (root / "custom_sources" / "c.py").write_text("x = 1\n")
    dango = root / ".dango"
    dango.mkdir()
    for name in ("project.yml", "auth.yml", "schedules.yml", "monitors.yml"):
        (dango / name).write_text(f"{name}: true\n")
    make_sqlite(dango / "auth.db", "auth")
    make_sqlite(dango / "dango.db", "meta")
    make_sqlite(dango / "scheduler.db", "jobs")
    loaded = root / ".dlt" / "pipelines" / "src" / "load" / "loaded" / "p1" / "completed_jobs"
    loaded.mkdir(parents=True)
    (loaded / "rows.insert_values.gz").write_bytes(b"row-data")
    return root


_EXPECTED_MEMBERS = (
    "data/warehouse.duckdb",
    "data/uploads/x.csv",
    "data/seeds/s.csv",
    "notebooks/n.py",
    "scripts/s.py",
    ".dango/auth.yml",
    ".dango/schedules.yml",
    ".dango/monitors.yml",
    ".dango/dango.db",
    ".dango/scheduler.db",
)


@pytest.fixture(params=["server", "scheduled"])
def creator(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _create(creator: str, tmp_path: Path, project: Path) -> tuple[Path, dict, list[str]]:
    if creator == "server":
        archive, members, warnings, _ssh = run_server_archive(tmp_path, project)
        return archive, members, warnings
    return run_scheduled_archive(tmp_path, project, checkpoint=lambda: [])


@pytest.mark.unit
def test_data_uploads_in_backup(creator: str, tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    _archive, members, _warnings = _create(creator, tmp_path, project)
    missing = [m for m in _EXPECTED_MEMBERS if m not in members]
    assert not missing, f"{creator} backup lacks {missing}"


@pytest.mark.unit
def test_dlt_pipelines_not_in_backup(creator: str, tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    _archive, members, _warnings = _create(creator, tmp_path, project)
    leaked = [m for m in members if m.startswith(".dlt/")]
    assert not leaked, f"{creator} backup holds dlt load copies: {leaked}"


@pytest.mark.unit
def test_wal_files_excluded_from_archive(creator: str, tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    (project / "data" / "warehouse.duckdb.wal").write_bytes(b"wal")
    (project / "data" / "export.tmp").write_bytes(b"partial")
    (project / "data" / ".tmp").mkdir()
    (project / "data" / ".tmp" / "spill.bin").write_bytes(b"spill")
    _archive, members, _warnings = _create(creator, tmp_path, project)
    assert "data/warehouse.duckdb" in members
    assert "data/uploads/x.csv" in members, "data/ is not backed up as a directory"
    unwanted = [m for m in members if m.endswith((".wal", ".tmp")) or "/.tmp" in m]
    assert not unwanted, f"{creator} archive holds WAL/temp files: {unwanted}"


@pytest.mark.unit
def test_scheduled_backup_checkpoints_scheduler_db(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    (project / ".dango" / "scheduler.db").unlink()
    make_sqlite_with_leftover_wal(project / ".dango" / "scheduler.db")
    archive, members, _warnings = run_scheduled_archive(tmp_path, project)
    assert ".dango/scheduler.db" in members
    restored = tmp_path / "restored-scheduler.db"
    restored.write_bytes(archive_text(archive, ".dango/scheduler.db"))
    conn = sqlite3.connect(str(restored))
    try:
        values = sorted(r[0] for r in conn.execute("SELECT n FROM u"))
    finally:
        conn.close()
    assert values == ["live", "old"], "scheduler.db WAL was not checkpointed before the copy"


@pytest.mark.unit
def test_server_backup_checkpoints_every_database(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import create_backup
    from tests.factories.backup_shell import ShellSSH

    project = _project(tmp_path / "project")
    ssh = ShellSSH(tmp_path / "shim")
    (tmp_path / "staging-root").mkdir()
    with (
        patch("dango.platform.cloud.backup.PROJECT_DIR", str(project)),
        patch("dango.platform.cloud.backup.BACKUP_DIR", str(tmp_path / "backups")),
        patch(
            "dango.platform.cloud.backup.BACKUP_STAGING_ROOT",
            str(tmp_path / "staging-root"),
            create=True,
        ),
        patch("dango.platform.cloud.backup.stop_services"),
        patch("dango.platform.cloud.backup.start_services"),
        patch("dango.platform.cloud.backup._get_metabase_volume_path", return_value=None),
    ):
        create_backup(ssh)
    for db in (".dango/auth.db", ".dango/dango.db", ".dango/scheduler.db"):
        assert any(f"{project}/{db}" in c and "checkpoint" in c for c in ssh.commands), (
            f"no checkpoint ran for {db}"
        )


# ---------------------------------------------------------------------------
# Links and special files in dango-writable trees are never followed (root copies them)
# ---------------------------------------------------------------------------

_WRITABLE_TREES = ("dbt/models", "scripts", "custom_sources", "notebooks", "data/uploads")


@pytest.fixture
def bounded_writes() -> Iterator[None]:
    """Cap file writes at 64 MB so a creator reading a device fails fast instead of filling the disk."""
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    limit = 64 * 1024 * 1024
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, previous)


@pytest.mark.unit
def test_backup_skips_symlinks_out_of_writable_trees(creator: str, tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    secret = tmp_path / "outside" / "shadow"
    secret.parent.mkdir()
    secret.write_text("TOP-SECRET-CONTENT")
    for tree in _WRITABLE_TREES:
        (project / tree / "leak.txt").symlink_to(secret)
    (project / "notebooks" / "linked-dir").symlink_to(secret.parent, target_is_directory=True)
    # A backed-up top-level file replaced by a link.
    (project / ".dango" / "monitors.yml").unlink()
    (project / ".dango" / "monitors.yml").symlink_to(secret)

    archive, members, warnings = _create(creator, tmp_path, project)

    links = [m for m, info in members.items() if not (info.isfile() or info.isdir())]
    assert not links, f"{creator} archive holds links/special files: {links}"
    for rel, info in members.items():
        if info.isfile():
            assert b"TOP-SECRET-CONTENT" not in archive_text(archive, rel), rel
    for skipped in (*(f"{t}/leak.txt" for t in _WRITABLE_TREES), ".dango/monitors.yml"):
        assert any(skipped in w for w in warnings), f"no warning names {skipped}: {warnings}"
    assert archive_text(archive, "scripts/s.py") == b"print('hi')\n"
    assert members["notebooks/n.py"].isfile()


@pytest.mark.unit
def test_backup_skips_device_symlinks(creator: str, tmp_path: Path, bounded_writes: None) -> None:
    project = _project(tmp_path / "project")
    (project / "scripts" / "zero").symlink_to("/dev/zero")
    (project / ".dango" / "schedules.yml").unlink()
    (project / ".dango" / "schedules.yml").symlink_to("/dev/zero")

    _archive, members, warnings = _create(creator, tmp_path, project)

    assert "scripts/zero" not in members
    assert ".dango/schedules.yml" not in members
    assert any("scripts/zero" in w for w in warnings), warnings
    assert any(".dango/schedules.yml" in w for w in warnings), warnings
    assert "scripts/s.py" in members


# ---------------------------------------------------------------------------
# data/ on separate storage: followed only into a directory the service user owns
# ---------------------------------------------------------------------------


def _root_owned_dir() -> Path:
    for candidate in ("/var/empty", "/usr/share/zoneinfo", "/etc"):
        path = Path(candidate)
        if path.is_dir() and path.stat().st_uid == 0:
            return path
    raise AssertionError("no root-owned directory found for the test")


def _with_linked_data(tmp_path: Path, target: Path) -> Path:
    project = _project(tmp_path / "project")
    real_data = project / "data"
    if target != real_data:
        shutil.rmtree(real_data)
    (project / "data").symlink_to(target, target_is_directory=True)
    return project


@pytest.mark.unit
def test_data_symlink_to_service_user_storage_is_backed_up(creator: str, tmp_path: Path) -> None:
    storage = tmp_path / "block-storage"
    _project(tmp_path / "seed")
    shutil.copytree(tmp_path / "seed" / "data", storage)
    secret = tmp_path / "outside-secret"
    secret.write_text("TOP-SECRET-CONTENT")
    (storage / "uploads" / "leak.csv").symlink_to(secret)
    project = _with_linked_data(tmp_path, storage)

    with patch(f"{_BR}.BACKUP_SOURCE_OWNER", getpass.getuser()):
        archive, members, warnings = _create(creator, tmp_path, project)

    assert "data/warehouse.duckdb" in members
    assert archive_text(archive, "data/uploads/x.csv") == b"a,b\n1,2\n"
    assert "data/uploads/leak.csv" not in members
    assert any("data/uploads/leak.csv" in w for w in warnings), warnings


@pytest.mark.unit
def test_data_symlink_to_foreign_directory_fails_the_backup(creator: str, tmp_path: Path) -> None:
    from dango.exceptions import CloudProvisioningError

    project = _with_linked_data(tmp_path, _root_owned_dir())

    with (
        patch(f"{_BR}.BACKUP_SOURCE_OWNER", getpass.getuser()),
        pytest.raises(CloudProvisioningError, match="data"),
    ):
        _create(creator, tmp_path, project)
