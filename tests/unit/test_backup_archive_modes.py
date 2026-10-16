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
