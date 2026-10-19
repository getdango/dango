"""tests/unit/test_backup_archive_modes.py

Scheduled backup archives keep source file modes; the archive itself is private; backup enable is version-gated.
"""

from __future__ import annotations

import os
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project tree with typical modes, and scheduled_backup pointed at it."""
    import dango.platform.cloud.scheduled_backup as sb

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / ".dango").mkdir()
    (project / "custom_sources").mkdir()
    warehouse = project / "data" / "warehouse.duckdb"
    warehouse.write_bytes(b"duckdb")
    warehouse.chmod(0o644)
    tool = project / "custom_sources" / "run.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (project / ".dango" / "project.yml").write_text("project: {}\n")

    backups = tmp_path / "backups" / "deploy"
    monkeypatch.setattr(sb, "PROJECT_DIR", project)
    monkeypatch.setattr(sb, "BACKUP_DIR", backups)
    monkeypatch.setattr(sb, "_stop_services", lambda: None)
    monkeypatch.setattr(sb, "_start_services", lambda: None)
    monkeypatch.setattr(sb, "_checkpoint_databases", lambda: [])
    monkeypatch.setattr(sb, "_get_metabase_volume_path", lambda: None)
    yield tmp_path


def _make_archive(umask: int) -> Path:
    from dango.platform.cloud.scheduled_backup import _create_local_archive

    previous = os.umask(umask)
    try:
        archive, _manifest, _warnings = _create_local_archive("scheduled")
    finally:
        os.umask(previous)
    return archive


def _member_modes(archive: Path) -> dict[str, int]:
    with tarfile.open(archive) as tar:
        return {
            m.name.split("/", 1)[1]: stat.S_IMODE(m.mode)
            for m in tar.getmembers()
            if m.isfile() and "/" in m.name
        }


@pytest.mark.unit
class TestArchiveModes:
    def test_archive_and_manifest_are_private(self, fake_server: Path) -> None:
        archive = _make_archive(0o022)
        manifest = archive.with_name(archive.name.replace(".tar.gz", ".json"))
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600

    def test_members_keep_source_modes_under_a_restrictive_umask(self, fake_server: Path) -> None:
        modes = _member_modes(_make_archive(0o077))
        assert modes["data/warehouse.duckdb"] == 0o644
        assert modes["custom_sources/run.sh"] == 0o755

    def test_restore_to_a_fresh_server_leaves_the_warehouse_world_readable(
        self, fake_server: Path
    ) -> None:
        archive = _make_archive(0o077)
        fresh = fake_server / "fresh"
        fresh.mkdir()
        # Extraction filters exist from 3.10.12/3.11.4; "tar" keeps member modes.
        kwargs = {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
        with tarfile.open(archive) as tar:
            tar.extractall(fresh, **kwargs)
        (restored,) = fresh.glob("*/data/warehouse.duckdb")
        assert stat.S_IMODE(restored.stat().st_mode) & 0o004, oct(restored.stat().st_mode)


# ---------------------------------------------------------------------------
# `remote backup enable` writes the hardened unit only to servers whose code reads the keys itself
# ---------------------------------------------------------------------------


def _enable(installed: str) -> tuple[MagicMock, str]:
    from dango.cli.commands.remote_backup import backup_group

    def _exec(command: str, *args: object, **kwargs: object) -> CommandResult:
        if "import dango" in command:
            return CommandResult(stdout=f"{installed}\n", stderr="", exit_code=0)
        if "exec cat" in command:  # the service-user read of the server .env
            return CommandResult(stdout="SPACES_ACCESS_KEY=k\n", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    with patch(
        "dango.cli.commands.remote_backup._load_cloud_config_with_ssh_or_fail",
        return_value=(MagicMock(), ssh),
    ):
        result = CliRunner().invoke(backup_group, ["enable"], catch_exceptions=False)
    return ssh, result.output


def _written_unit(ssh: MagicMock) -> str:
    writes = {c.args[0]: c.args[1] for c in ssh.write_remote_file.call_args_list}
    return writes["/etc/systemd/system/dango-backup.service"]


@pytest.mark.unit
class TestBackupEnableVersionGate:
    def test_old_server_gets_the_legacy_unit_and_an_upgrade_hint(self) -> None:
        ssh, output = _enable("1.0.13")
        unit = _written_unit(ssh)
        assert "EnvironmentFile=-/srv/dango/project/.env" in unit
        assert "dango remote upgrade" in " ".join(output.split())

    def test_current_server_gets_the_hardened_unit(self) -> None:
        from dango.platform.cloud._server_templates import SYSTEMD_BACKUP_SERVICE

        ssh, _output = _enable("1.0.14")
        assert _written_unit(ssh) == SYSTEMD_BACKUP_SERVICE
        assert "EnvironmentFile" not in SYSTEMD_BACKUP_SERVICE

    def test_version_probe_runs_isolated(self) -> None:
        ssh, _output = _enable("1.0.14")
        probes = [c.args[0] for c in ssh.exec_command.call_args_list if "import dango" in c.args[0]]
        assert probes and all("/srv/dango/venv/bin/python -I " in p for p in probes), probes


# ---------------------------------------------------------------------------
# Root never creates set-id files, never uses a predictable staging path, never
# writes the archive through a planted link
# ---------------------------------------------------------------------------

_SPECIAL_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
_FROZEN = "20991231-235959"


def _all_member_modes(archive: Path) -> dict[str, int]:
    with tarfile.open(archive) as tar:
        return {m.name: m.mode for m in tar.getmembers() if not m.issym()}


@pytest.mark.unit
class TestNoSpecialBits:
    def test_staged_copies_drop_setuid_setgid_and_sticky(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _copy_keeping_modes

        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        tool = src / "sub" / "tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o4755)
        (src / "sub").chmod(0o2755)
        (src / "shared").mkdir()
        (src / "shared").chmod(0o1777)
        single = tmp_path / "single"
        single.write_text("x")
        single.chmod(0o4755)

        dest = tmp_path / "dest"
        dest.mkdir()
        _copy_keeping_modes(src, dest, step="t")
        _copy_keeping_modes(single, tmp_path / "single-copy", step="t")

        assert stat.S_IMODE((dest / "sub" / "tool").stat().st_mode) == 0o755
        assert stat.S_IMODE((tmp_path / "single-copy").stat().st_mode) == 0o755
        for path in (dest / "sub", dest / "shared", dest / "sub" / "tool"):
            assert not path.stat().st_mode & _SPECIAL_BITS, (path, oct(path.stat().st_mode))

    def test_archive_members_carry_no_special_bits(self, fake_server: Path) -> None:
        tool = fake_server / "project" / "custom_sources" / "run.sh"
        tool.chmod(0o4755)
        archive = _make_archive(0o022)
        for name, mode in _all_member_modes(archive).items():
            assert not mode & _SPECIAL_BITS, (name, oct(mode))
        assert _member_modes(archive)["custom_sources/run.sh"] == 0o755


@pytest.fixture
def frozen_name(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Freeze the backup timestamp so the predictable names are known in advance."""
    import shutil

    import dango.platform.cloud.scheduled_backup as sb

    monkeypatch.setattr(sb.time, "strftime", lambda fmt, *a: _FROZEN)
    name = f"backup-{_FROZEN}"
    yield name
    planted = Path("/tmp") / name
    if planted.is_symlink() or planted.is_file():
        planted.unlink()
    elif planted.exists():
        shutil.rmtree(planted)


@pytest.mark.unit
class TestPrivateStaging:
    def test_staging_is_fresh_and_private(
        self, fake_server: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dango.platform.cloud.scheduled_backup as sb

        seen: list[Path] = []
        real_copy = sb._copy_keeping_modes

        def _spy(src: Path, dest: Path, *, step: str) -> None:
            seen.append(dest)
            real_copy(src, dest, step=step)

        monkeypatch.setattr(sb, "_copy_keeping_modes", _spy)
        _make_archive(0o022)
        assert seen
        for dest in seen:
            assert "/backup-" not in str(dest), dest
            assert "/dango-backup." in str(dest), dest

    def test_staging_root_is_mode_0700(
        self, fake_server: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tempfile

        import dango.platform.cloud.scheduled_backup as sb

        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def _spy(
            suffix: str | None = None, prefix: str | None = None, dir: str | None = None
        ) -> str:
            path = real_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
            made.append(path)
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", _spy)
        modes: list[int] = []
        real_copy = sb._copy_keeping_modes

        def _copy(src: Path, dest: Path, *, step: str) -> None:
            modes.extend(stat.S_IMODE(Path(p).stat().st_mode) for p in made)
            real_copy(src, dest, step=step)

        monkeypatch.setattr(sb, "_copy_keeping_modes", _copy)
        _make_archive(0o022)
        assert made and modes and set(modes) == {0o700}, (made, [oct(m) for m in modes])

    def test_preplanted_predictable_staging_dir_is_never_used(
        self, fake_server: Path, frozen_name: str
    ) -> None:
        planted = Path("/tmp") / frozen_name
        planted.mkdir()
        _make_archive(0o022)
        assert list(planted.iterdir()) == []

    def test_preplanted_predictable_staging_symlink_is_never_followed(
        self, fake_server: Path, frozen_name: str
    ) -> None:
        target = fake_server / "attacker"
        target.mkdir()
        (Path("/tmp") / frozen_name).symlink_to(target)
        _make_archive(0o022)
        assert list(target.iterdir()) == []


@pytest.mark.unit
class TestArchivePathNotFollowed:
    def test_symlink_at_archive_path_fails_without_writing_through(
        self, fake_server: Path, frozen_name: str
    ) -> None:
        import dango.platform.cloud.scheduled_backup as sb

        victim = fake_server / "victim"
        victim.write_text("original")
        sb.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (sb.BACKUP_DIR / f"{frozen_name}.tar.gz").symlink_to(victim)
        with pytest.raises((OSError, CloudProvisioningError)):
            _make_archive(0o022)
        assert victim.read_text() == "original"

    def test_existing_file_at_archive_path_is_not_reused(
        self, fake_server: Path, frozen_name: str
    ) -> None:
        import dango.platform.cloud.scheduled_backup as sb

        sb.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        existing = sb.BACKUP_DIR / f"{frozen_name}.tar.gz"
        existing.write_text("planted")
        existing.chmod(0o666)
        with pytest.raises((OSError, CloudProvisioningError)):
            _make_archive(0o022)
        assert existing.read_text() == "planted"


@pytest.mark.unit
class TestRestoreExtraction:
    def test_restore_extracts_without_preserving_permissions(self, tmp_path: Path) -> None:
        from tests.factories.backup_shell import run_spaces_restore

        content = tmp_path / "backup-x"
        (content / ".dango").mkdir(parents=True)
        (content / ".dango" / "project.yml").write_text("project: {}\n")
        (tmp_path / "project").mkdir()

        _shim, events = run_spaces_restore(tmp_path, tmp_path / "project", content.name)

        (extract,) = [e for e in events if e.startswith("run: ") and "-xzf" in e]
        assert "--no-same-permissions" in extract, extract
