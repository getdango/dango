"""tests/unit/test_migrations.py

Tests for the database migration framework in dango/migrations/.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from dango.exceptions import MigrationApplicationError, MigrationDiscoveryError
from dango.migrations import (
    MigrationInfo,
    MigrationRunner,
    MigrationStatus,
    apply_all_pending,
    get_all_status,
)
from dango.migrations.runner import AppliedMigration


def _write_migration(directory: Path, version: int, description: str, sql: str = "") -> Path:
    """Create a properly formatted migration file."""
    prefix = f"{version:03d}"
    slug = description.lower().replace(" ", "_")
    path = directory / f"{prefix}_{slug}.py"
    stmt = f"conn.execute({sql!r})" if sql else "pass"
    path.write_text(
        textwrap.dedent(f"""\
        from __future__ import annotations
        import sqlite3
        VERSION = {version}
        DESCRIPTION = {description!r}
        def upgrade(conn: sqlite3.Connection) -> None:
            {stmt}
    """)
    )
    return path


def _make_runner(tmp_path: Path) -> tuple[Path, Path, MigrationRunner]:
    """Create migrations dir, db_path, and runner."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    db_path = tmp_path / "test.db"
    runner = MigrationRunner(db_path=db_path, db_name="test", migrations_dir=mdir)
    return mdir, db_path, runner


@pytest.mark.unit
class TestDataclasses:
    """Tests for MigrationInfo and AppliedMigration dataclasses."""

    def test_migration_info_construction(self, tmp_path: Path) -> None:
        info = MigrationInfo(version=1, description="test", file_path=tmp_path / "001_test.py")
        assert info.version == 1
        assert info.description == "test"

    def test_migration_info_frozen(self, tmp_path: Path) -> None:
        info = MigrationInfo(version=1, description="test", file_path=tmp_path / "001_test.py")
        with pytest.raises(AttributeError):
            info.version = 2  # type: ignore[misc]

    def test_applied_migration_construction(self) -> None:
        am = AppliedMigration(version=1, description="test", applied_at="2026-01-01")
        assert am.version == 1
        assert am.applied_at == "2026-01-01"

    def test_applied_migration_frozen(self) -> None:
        am = AppliedMigration(version=1, description="test", applied_at="2026-01-01")
        with pytest.raises(AttributeError):
            am.version = 2  # type: ignore[misc]


@pytest.mark.unit
class TestMigrationDiscovery:
    """Tests for MigrationRunner.discover_migrations()."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        assert runner.discover_migrations() == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        runner = MigrationRunner(
            db_path=tmp_path / "test.db", db_name="test", migrations_dir=tmp_path / "nope"
        )
        assert runner.discover_migrations() == []

    def test_single_migration(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Create users", "CREATE TABLE users (id INTEGER)")
        found = runner.discover_migrations()
        assert len(found) == 1
        assert found[0].version == 1
        assert found[0].description == "Create users"

    def test_multiple_migrations_sorted(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 3, "Third")
        _write_migration(mdir, 1, "First")
        _write_migration(mdir, 2, "Second")
        assert [m.version for m in runner.discover_migrations()] == [1, 2, 3]

    def test_skips_init_py_and_non_python(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        (mdir / "__init__.py").write_text("")
        (mdir / "README.md").write_text("docs")
        _write_migration(mdir, 1, "Real migration")
        assert len(runner.discover_migrations()) == 1

    def test_duplicate_version_raises(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "First")
        (mdir / "001_duplicate.py").write_text(
            'VERSION = 1\nDESCRIPTION = "Dup"\ndef upgrade(c): pass\n'
        )
        with pytest.raises(MigrationDiscoveryError, match="Duplicate migration version"):
            runner.discover_migrations()

    def test_missing_version_attr_raises(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        (mdir / "001_bad.py").write_text('DESCRIPTION = "x"\ndef upgrade(c): pass\n')
        with pytest.raises(MigrationDiscoveryError, match="missing required attribute 'VERSION'"):
            runner.discover_migrations()

    def test_missing_description_attr_raises(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        (mdir / "001_bad.py").write_text("VERSION = 1\ndef upgrade(c): pass\n")
        with pytest.raises(
            MigrationDiscoveryError, match="missing required attribute 'DESCRIPTION'"
        ):
            runner.discover_migrations()

    def test_missing_upgrade_func_raises(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        (mdir / "001_bad.py").write_text('VERSION = 1\nDESCRIPTION = "x"\n')
        with pytest.raises(MigrationDiscoveryError, match="missing required attribute 'upgrade'"):
            runner.discover_migrations()

    def test_version_mismatch_raises(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        (mdir / "001_bad.py").write_text(
            'VERSION = 2\nDESCRIPTION = "wrong"\ndef upgrade(c): pass\n'
        )
        with pytest.raises(MigrationDiscoveryError, match="does not match filename prefix"):
            runner.discover_migrations()


@pytest.mark.unit
class TestMigrationRunner:
    """Tests for MigrationRunner apply/status operations."""

    def test_creates_migrations_table(self, tmp_path: Path) -> None:
        _, db_path, runner = _make_runner(tmp_path)
        assert runner.get_applied_versions() == []
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_migrations'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1

    def test_apply_single_migration(self, tmp_path: Path) -> None:
        mdir, db_path, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Create table", "CREATE TABLE users (id INTEGER PRIMARY KEY)")
        applied = runner.apply_pending()
        assert len(applied) == 1
        assert applied[0].version == 1
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchall()
        conn.close()
        assert len(tables) == 1

    def test_apply_multiple_in_order(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Create users", "CREATE TABLE users (id INTEGER)")
        _write_migration(mdir, 2, "Create roles", "CREATE TABLE roles (id INTEGER, name TEXT)")
        applied = runner.apply_pending()
        assert [m.version for m in applied] == [1, 2]
        assert runner.current_version() == 2

    def test_skips_already_applied(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Create table", "CREATE TABLE users (id INTEGER)")
        runner.apply_pending()
        _write_migration(mdir, 2, "Add column", "ALTER TABLE users ADD COLUMN name TEXT")
        applied = runner.apply_pending()
        assert len(applied) == 1
        assert applied[0].version == 2

    def test_records_version_and_timestamp(self, tmp_path: Path) -> None:
        mdir, db_path, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Test migration")
        runner.apply_pending()
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT version, description, applied_at FROM _migrations").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "Test migration"
        assert rows[0][2]  # timestamp not empty

    def test_failed_migration_rolls_back(self, tmp_path: Path) -> None:
        mdir, db_path, runner = _make_runner(tmp_path)
        (mdir / "001_bad.py").write_text(
            textwrap.dedent("""\
            VERSION = 1
            DESCRIPTION = "Will fail"
            def upgrade(conn):
                conn.execute("CREATE TABLE foo (id INTEGER)")
                raise RuntimeError("boom")
        """)
        )
        with pytest.raises(MigrationApplicationError, match="boom"):
            runner.apply_pending()
        conn = sqlite3.connect(str(db_path))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='foo'"
        ).fetchall()
        conn.close()
        assert len(tables) == 0
        assert runner.current_version() == 0

    def test_partial_failure_stops(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Good", "CREATE TABLE good (id INTEGER)")
        (mdir / "002_bad.py").write_text(
            textwrap.dedent("""\
            VERSION = 2
            DESCRIPTION = "Bad"
            def upgrade(conn):
                raise RuntimeError("fail")
        """)
        )
        _write_migration(mdir, 3, "Never reached")
        with pytest.raises(MigrationApplicationError):
            runner.apply_pending()
        assert runner.current_version() == 1
        assert runner.get_applied_versions() == [1]

    def test_current_version_zero_when_none(self, tmp_path: Path) -> None:
        _, _, runner = _make_runner(tmp_path)
        assert runner.current_version() == 0

    def test_status_returns_full_info(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "Applied")
        _write_migration(mdir, 2, "Pending")
        runner.apply_one(runner.discover_migrations()[0])
        status = runner.status()
        assert isinstance(status, MigrationStatus)
        assert status.db_name == "test"
        assert status.current_version == 1
        assert len(status.applied) == 1
        assert len(status.pending) == 1
        assert status.pending[0].version == 2


@pytest.mark.unit
class TestMigrationRunnerEdgeCases:
    """Edge case tests for MigrationRunner."""

    def test_nonexistent_db_path_creates_file(self, tmp_path: Path) -> None:
        mdir = tmp_path / "migrations"
        mdir.mkdir()
        db_path = tmp_path / "subdir" / "test.db"
        _write_migration(mdir, 1, "Init", "CREATE TABLE t (id INTEGER)")
        runner = MigrationRunner(db_path=db_path, db_name="test", migrations_dir=mdir)
        runner.apply_pending()
        assert db_path.exists()
        assert runner.current_version() == 1

    def test_gaps_in_versions_allowed(self, tmp_path: Path) -> None:
        mdir, _, runner = _make_runner(tmp_path)
        _write_migration(mdir, 1, "First")
        _write_migration(mdir, 5, "Fifth")
        applied = runner.apply_pending()
        assert [m.version for m in applied] == [1, 5]
        assert runner.current_version() == 5

    def test_no_migration_files_is_noop(self, tmp_path: Path) -> None:
        _, _, runner = _make_runner(tmp_path)
        assert runner.apply_pending() == []
        assert runner.current_version() == 0


@pytest.mark.unit
class TestApplyAllPending:
    """Tests for the apply_all_pending() public API."""

    def _patch_base(self, monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
        import dango.migrations as mig_mod

        monkeypatch.setattr(mig_mod, "get_migrations_base_dir", lambda: base)

    def test_no_subdirectories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_base(monkeypatch, tmp_path)
        assert apply_all_pending(tmp_path) == {}

    def test_applies_to_all_databases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = tmp_path / "mig"
        base.mkdir()
        self._patch_base(monkeypatch, base)
        auth_dir = base / "auth"
        auth_dir.mkdir()
        _write_migration(auth_dir, 1, "Auth init", "CREATE TABLE users (id INTEGER)")
        sched_dir = base / "scheduler"
        sched_dir.mkdir()
        _write_migration(sched_dir, 1, "Sched init", "CREATE TABLE jobs (id INTEGER)")
        project = tmp_path / "project"
        (project / ".dango").mkdir(parents=True)
        result = apply_all_pending(project)
        assert len(result["auth"]) == 1
        assert len(result["scheduler"]) == 1
        assert (project / ".dango" / "auth.db").exists()
        assert (project / ".dango" / "scheduler.db").exists()

    def test_skips_pycache_and_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        base = tmp_path / "mig"
        base.mkdir()
        self._patch_base(monkeypatch, base)
        (base / "__pycache__").mkdir()
        (base / "empty_db").mkdir()
        assert apply_all_pending(tmp_path) == {}


@pytest.mark.unit
class TestGetAllStatus:
    """Tests for the get_all_status() public API."""

    def test_returns_statuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import dango.migrations as mig_mod

        base = tmp_path / "mig"
        base.mkdir()
        monkeypatch.setattr(mig_mod, "get_migrations_base_dir", lambda: base)
        auth_dir = base / "auth"
        auth_dir.mkdir()
        _write_migration(auth_dir, 1, "Auth init")
        project = tmp_path / "project"
        (project / ".dango").mkdir(parents=True)
        statuses = get_all_status(project)
        assert len(statuses) == 1
        assert statuses[0].db_name == "auth"
        assert len(statuses[0].pending) == 1


@pytest.mark.unit
class TestConfigVersionCheck:
    """Tests for config version validation in loader."""

    def test_supported_version_passes(self, tmp_path: Path) -> None:
        from dango.config.loader import ConfigLoader

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("version: '1.0'\nsources: []\n")
        loader = ConfigLoader(tmp_path)
        assert loader.load_sources_config().version == "1.0"

    def test_unsupported_version_raises(self, tmp_path: Path) -> None:
        from dango.config.loader import ConfigLoader
        from dango.exceptions import ConfigVersionError

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("version: '99.0'\nsources: []\n")
        loader = ConfigLoader(tmp_path)
        with pytest.raises(ConfigVersionError, match="99.0"):
            loader.load_sources_config()

    def test_validate_config_catches_version_error(self, tmp_path: Path) -> None:
        from dango.config.loader import ConfigLoader

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "project.yml").write_text(
            "project:\n  name: test\n  created_by: tester\n  purpose: testing\n"
        )
        (dango_dir / "sources.yml").write_text("version: '99.0'\nsources: []\n")
        loader = ConfigLoader(tmp_path)
        is_valid, errors = loader.validate_config()
        assert not is_valid
        assert any("99.0" in e for e in errors)
