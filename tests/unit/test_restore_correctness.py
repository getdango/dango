"""tests/unit/test_restore_correctness.py

Restore correctness: every file the manifest lists must be in the archive, a backup that holds
Metabase data is refused (before anything changes) when the server has no Metabase volume, and
restore warnings are always shown.  Real shell steps run through ``ShellSSH``.
"""

from __future__ import annotations

import inspect
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.remote import remote
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import backup, migrate
from dango.platform.cloud.backup_restore import extract_checked_archive
from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_shell import ShellSSH, run_spaces_restore

pytestmark = pytest.mark.unit

CONTENT = ["data/warehouse.duckdb", "notebooks"]


def _make_archive(
    tmp_path: Path, present: list[str], listed: list[str] | None, *, raw_manifest: str | None = None
) -> Path:
    """Archive ``backup-1/`` holding *present* files and a manifest listing *listed*."""
    top = tmp_path / "content" / "backup-1"
    for rel in present:
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_text("x")
    top.mkdir(parents=True, exist_ok=True)
    if raw_manifest is None:
        files = [{"path": p, "size_bytes": 1} for p in (present if listed is None else listed)]
        raw_manifest = json.dumps({"timestamp": "t", "files": files})
    (top / "manifest.json").write_text(raw_manifest)
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(top), arcname="backup-1")
    return archive


def _extract(tmp_path: Path, archive: Path) -> Path:
    staging = tmp_path / "stage"
    extract_checked_archive(ShellSSH(tmp_path / "shim"), str(archive), str(staging), CONTENT)
    return staging


# --- AF-13: the manifest's file list is enforced -------------------------------------------------


def test_manifest_listing_a_file_the_archive_lacks_is_refused_and_staging_removed(
    tmp_path: Path,
) -> None:
    archive = _make_archive(
        tmp_path, ["notebooks/a.py"], ["data/warehouse.duckdb", "notebooks/a.py"]
    )
    with pytest.raises(CloudProvisioningError, match=r"data/warehouse\.duckdb"):
        _extract(tmp_path, archive)
    assert not (tmp_path / "stage").exists()


def test_a_complete_archive_extracts(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, ["data/warehouse.duckdb", "notebooks/a.py"], None)
    assert (_extract(tmp_path, archive) / "data" / "warehouse.duckdb").is_file()


def test_documented_optional_files_may_be_absent(tmp_path: Path) -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    assert "metabase/metabase.db.trace.db" in checks.OPTIONAL_MANIFEST_FILES
    archive = _make_archive(
        tmp_path,
        ["notebooks/a.py"],
        ["notebooks/a.py", "metabase/metabase.db.trace.db"],
    )
    assert (_extract(tmp_path, archive) / "notebooks" / "a.py").is_file()


@pytest.mark.parametrize("raw", ["{}", '{"files": []}', '{"timestamp": "t"}'])
def test_manifests_without_a_file_list_still_restore(tmp_path: Path, raw: str) -> None:
    archive = _make_archive(tmp_path, ["notebooks/a.py"], None, raw_manifest=raw)
    assert (_extract(tmp_path, archive) / "notebooks" / "a.py").is_file()


@pytest.mark.parametrize("raw", ["not json", "[]", '{"files": "x"}', '{"files": [1]}'])
def test_an_unreadable_manifest_is_refused(tmp_path: Path, raw: str) -> None:
    archive = _make_archive(tmp_path, ["notebooks/a.py"], None, raw_manifest=raw)
    with pytest.raises(CloudProvisioningError, match="manifest"):
        _extract(tmp_path, archive)
    assert not (tmp_path / "stage").exists()


def test_the_refusal_names_a_bounded_number_of_files(tmp_path: Path) -> None:
    from dango.platform.cloud.backup import BACKUP_FILES

    listed = list(BACKUP_FILES)
    archive = _make_archive(tmp_path, ["data/warehouse.duckdb"], ["data/warehouse.duckdb", *listed])
    with pytest.raises(CloudProvisioningError) as exc:
        _extract(tmp_path, archive)
    text = str(exc.value)
    assert str(len(listed)) in text and "more" in text and text.count(".dango/") <= 5


HOSTILE_NAMES = [
    "notebooks/a",  # the fragment a newline in a file name leaves in a manifest
    "notebooks/a b\nc.py",
    "notebooks/esc\x1b[31m.py",
    "notebooks/ spaced name.py",
    "notebooks/-leading-dash.py",
    "notebooks/a[/].py",
]


def test_a_notebook_file_name_cannot_make_a_backup_unrestorable(tmp_path: Path) -> None:
    """Only fixed-name entries are enforced; names a notebook user picks are never trusted."""
    archive = _make_archive(
        tmp_path, ["data/warehouse.duckdb"], ["data/warehouse.duckdb", *HOSTILE_NAMES]
    )
    assert (_extract(tmp_path, archive) / "data" / "warehouse.duckdb").is_file()


def test_the_real_file_with_a_newline_in_its_name_restores(tmp_path: Path) -> None:
    name = "notebooks/a b\nc.py"
    archive = _make_archive(
        tmp_path, ["data/warehouse.duckdb", name], ["data/warehouse.duckdb", "notebooks/a", name]
    )
    assert (_extract(tmp_path, archive) / name).is_file()


def test_the_local_check_ignores_names_a_notebook_user_picks(tmp_path: Path) -> None:
    from dango.platform.cloud.backup_restore_checks import check_local_manifest

    stage = tmp_path / "stage"
    (stage / "data").mkdir(parents=True)
    (stage / "data" / "warehouse.duckdb").write_text("x")
    files = [{"path": p, "size_bytes": 1} for p in ["data/warehouse.duckdb", *HOSTILE_NAMES]]
    (stage / "manifest.json").write_text(json.dumps({"files": files}))
    check_local_manifest(stage)
    (stage / "data" / "warehouse.duckdb").unlink()
    with pytest.raises(CloudProvisioningError, match=r"data/warehouse\.duckdb"):
        check_local_manifest(stage)


def test_a_missing_metabase_database_listed_in_the_manifest_is_refused(tmp_path: Path) -> None:
    archive = _make_archive(
        tmp_path, ["notebooks/a.py"], ["notebooks/a.py", "metabase/metabase.db.mv.db"]
    )
    with pytest.raises(CloudProvisioningError, match=r"metabase\.db\.mv\.db"):
        _extract(tmp_path, archive)


def test_the_spaces_restore_refuses_an_incomplete_archive_before_stopping_anything(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top).mkdir()
    (tmp_path / top / "notebooks").mkdir()
    (tmp_path / top / "notebooks" / "a.py").write_text("x")
    (tmp_path / top / "manifest.json").write_text(
        json.dumps({"files": [{"path": "data/warehouse.duckdb", "size_bytes": 1}]})
    )
    events: list[str] = []
    with pytest.raises(CloudProvisioningError, match=r"data/warehouse\.duckdb"):
        run_spaces_restore(tmp_path, project, top, events=events)
    assert "stop" not in events


# --- AF-14: a missing Metabase volume is a refusal, not a silent skip ----------------------------


def _h2_archive(tmp_path: Path) -> tuple[Path, Path]:
    top = tmp_path / "content" / "backup-1"
    (top / "notebooks").mkdir(parents=True)
    (top / "notebooks" / "a.py").write_text("A\n")
    (top / "metabase").mkdir()
    (top / "metabase" / "metabase.db.mv.db").write_text("H2")
    (top / "manifest.json").write_text("{}")
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(top), arcname="backup-1")
    project = tmp_path / "project"
    (project / "notebooks").mkdir(parents=True)
    return archive, project


def _restore(
    tmp_path: Path, archive: Path, project: Path, volume: Path | None, **kwargs: bool
) -> tuple[MagicMock, MagicMock, backup.RestoreResult]:
    safety, stop = MagicMock(archive_path="s"), MagicMock()
    with (
        patch.object(backup, "PROJECT_DIR", str(project)),
        patch.object(backup, "RESTORE_STAGING_ROOT", str(tmp_path / "root")),
        patch.object(backup, "create_backup", safety),
        patch.object(backup, "stop_services", stop),
        patch.object(backup, "start_services"),
        patch.object(backup, "verify_health", return_value=True),
        patch.object(
            backup, "_get_metabase_volume_path", return_value=str(volume) if volume else None
        ),
        patch("dango.platform.cloud.runner_reapply.reapply", return_value=None),
    ):
        result = backup.restore_from_archive(ShellSSH(tmp_path / "shim"), str(archive), **kwargs)
    return safety, stop, result


def test_restore_without_a_metabase_volume_is_refused_before_anything_changes(
    tmp_path: Path,
) -> None:
    archive, project = _h2_archive(tmp_path)
    with pytest.raises(CloudProvisioningError, match="Metabase") as exc:
        _restore(tmp_path, archive, project, None)
    assert "Nothing was restored" in str(exc.value) and "dango-web" in str(exc.value)
    assert (project / "notebooks").is_dir() and not (project / "notebooks" / "a.py").exists()
    root = tmp_path / "root"
    assert not root.exists() or not list(root.glob("dango-restore-staging*"))


def test_the_refusal_comes_before_the_safety_backup_and_the_service_stop(tmp_path: Path) -> None:
    archive, project = _h2_archive(tmp_path)
    safety, stop = MagicMock(), MagicMock()
    with (
        patch.object(backup, "PROJECT_DIR", str(project)),
        patch.object(backup, "RESTORE_STAGING_ROOT", str(tmp_path / "root")),
        patch.object(backup, "create_backup", safety),
        patch.object(backup, "stop_services", stop),
        patch.object(backup, "_get_metabase_volume_path", return_value=None),
        pytest.raises(CloudProvisioningError),
    ):
        backup.restore_from_archive(ShellSSH(tmp_path / "shim"), str(archive))
    safety.assert_not_called()
    stop.assert_not_called()


def test_restore_with_a_metabase_volume_copies_the_database(tmp_path: Path) -> None:
    archive, project = _h2_archive(tmp_path)
    volume = tmp_path / "volume"
    volume.mkdir()
    _restore(tmp_path, archive, project, volume)
    assert (volume / "metabase.db.mv.db").read_text() == "H2"
    assert (project / "notebooks" / "a.py").read_text() == "A\n"


def test_a_backup_without_metabase_data_restores_without_a_volume(tmp_path: Path) -> None:
    top = tmp_path / "content" / "backup-1"
    (top / "notebooks").mkdir(parents=True)
    (top / "notebooks" / "a.py").write_text("A\n")
    (top / "manifest.json").write_text("{}")
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(top), arcname="backup-1")
    project = tmp_path / "project"
    (project / "notebooks").mkdir(parents=True)
    _, _, result = _restore(tmp_path, archive, project, None)
    assert not any("Metabase" in w for w in result.warnings)


def test_migrate_keeps_the_warning_instead_of_refusing_on_a_fresh_server(tmp_path: Path) -> None:
    archive, project = _h2_archive(tmp_path)
    _, _, result = _restore(tmp_path, archive, project, None, require_metabase_volume=False)
    assert any("H2 restore skipped" in w for w in result.warnings)
    assert "require_metabase_volume=False" in inspect.getsource(migrate)


def test_the_spaces_restore_refuses_without_a_metabase_volume_before_stopping(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top / "metabase").mkdir(parents=True)
    (tmp_path / top / "metabase" / "metabase.db.mv.db").write_text("H2")
    (tmp_path / top / "manifest.json").write_text("{}")
    events: list[str] = []
    with pytest.raises(CloudProvisioningError, match="Metabase"):
        run_spaces_restore(tmp_path, project, top, events=events)
    assert "stop" not in events


def test_the_spaces_restore_with_a_volume_copies_the_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    volume = tmp_path / "volume"
    volume.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top / "metabase").mkdir(parents=True)
    (tmp_path / top / "metabase" / "metabase.db.mv.db").write_text("H2")
    (tmp_path / top / "manifest.json").write_text("{}")
    run_spaces_restore(tmp_path, project, top, metabase_vol=volume)
    assert (volume / "metabase.db.mv.db").read_text() == "H2"


def test_the_spaces_restore_returns_its_warnings(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    top = "backup-20260101-000000"
    (tmp_path / top / ".dango").mkdir(parents=True)
    (tmp_path / top / ".dango" / "auth.db").write_text("x")
    (tmp_path / top / "manifest.json").write_text("{}")
    note = "Runner permissions were not re-applied (verify: boom)."
    returned: list[list[str]] = []
    with patch("dango.platform.cloud.runner_reapply.reapply_local", return_value=[note]):
        run_spaces_restore(tmp_path, project, top, returned=returned)
    assert returned == [[note]]


# --- restore warnings are always shown ----------------------------------------------------------

_LOADER = "dango.config.loader.ConfigLoader"
_CTX = "dango.cli.utils.require_project_context"
_SSH = "dango.platform.cloud.ssh.SSHManager"


def _cli(tmp_path: Path, args: list[str], ssh: MagicMock) -> Result:
    cfg = MagicMock(droplet_id=42, droplet_ip="1.2.3.4", ssh_key_path=".dango/cloud_key")
    loader = MagicMock()
    loader.load_cloud_config.return_value = cfg
    with (
        patch(_CTX, return_value=tmp_path),
        patch(_LOADER, return_value=loader),
        patch(_SSH, return_value=ssh),
    ):
        return CliRunner().invoke(remote, args, obj={"project_root": tmp_path})


def _ssh(stdout: str = "") -> MagicMock:
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout=stdout, stderr="", exit_code=0)
    ssh.connect.return_value = ssh
    return ssh


def test_a_healthy_local_restore_still_shows_its_warnings(tmp_path: Path) -> None:
    backup_file = tmp_path / "backup-20260224-143000.tar.gz"
    backup_file.write_bytes(b"x")
    result = backup.RestoreResult(
        restored_from="/x",
        services_restarted=True,
        health_check_passed=True,
        duration_seconds=1.0,
        warnings=["Runner permissions were not re-applied (verify: boom)."],
    )
    with patch("dango.platform.cloud.backup.restore_from_archive", return_value=result):
        out = _cli(tmp_path, ["backup", "restore", str(backup_file), "--from-local", "-y"], _ssh())
    assert out.exit_code == 0
    assert "Restore complete" in out.output
    assert "Runner permissions were not re-applied" in out.output


def test_a_spaces_restore_shows_the_warnings_the_server_reports(tmp_path: Path) -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    line = f"{checks.RESTORE_WARNING_PREFIX}Runner permissions were not re-applied (verify: boom)."
    ssh = _ssh(f"progress noise\n{line}\n")
    out = _cli(tmp_path, ["backup", "restore", "backup-20260224-143000.tar.gz", "-y"], ssh)
    assert out.exit_code == 0
    assert "Restore complete" in out.output
    assert "Runner permissions were not re-applied" in out.output
    assert "progress noise" not in out.output
    sent = ssh.exec_command.call_args_list[-1].args[0]
    assert "restore_from_spaces" in sent and checks.RESTORE_WARNING_PREFIX.strip() in sent


def test_a_spaces_restore_strips_control_characters_from_server_text(tmp_path: Path) -> None:
    from dango.platform.cloud import backup_restore_checks as checks

    ssh = _ssh(f"{checks.RESTORE_WARNING_PREFIX}bad\x1b[31m text\n")
    out = _cli(tmp_path, ["backup", "restore", "backup-20260224-143000.tar.gz", "-y"], ssh)
    assert "bad\x1b" not in out.output and "bad[31m text" in out.output
