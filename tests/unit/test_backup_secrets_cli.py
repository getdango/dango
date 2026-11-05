"""tests/unit/test_backup_secrets_cli.py

Secrets are not in a backup: the names to re-enter after download or restore, and the reminder before destructive commands (G3F4).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.deploy import deploy
from dango.cli.commands.remote import remote
from tests.factories.backup_cli import (
    CTX,
    DOWNLOAD_HINT,
    LOADER,
    SSH,
    invoke,
    make_cfg,
    make_loader,
    make_ssh,
    plain,
)

# ---------------------------------------------------------------------------
# backup download / restore: secrets are not in the backup
# ---------------------------------------------------------------------------


def _archive(manifest: dict | None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if manifest is not None:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("backup-1/manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_MANIFEST = {"secret_names": ["API_KEY", "sources.s.client_secret"], "secrets_included": False}
_REENTER = "secrets are not in this backup; re-enter: API_KEY, sources.s.client_secret"


@pytest.mark.unit
class TestSecretsNotInBackupMessage:
    def test_download_from_spaces_names_what_to_re_enter(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.download.return_value = _archive(_MANIFEST)
        out = tmp_path / "b.tar.gz"
        with patch("dango.platform.cloud.spaces.SpacesClient", return_value=client):
            result = invoke(
                remote,
                ["backup", "download", "backup-20261101-020000.tar.gz", "-o", str(out)],
                tmp_path,
                make_cfg(),
                MagicMock(),
            )
        assert result.exit_code == 0, result.output
        assert _REENTER in plain(result.output)

    def test_download_from_spaces_with_an_older_backup(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.download.return_value = _archive({"files": []})
        out = tmp_path / "b.tar.gz"
        with patch("dango.platform.cloud.spaces.SpacesClient", return_value=client):
            result = invoke(
                remote,
                ["backup", "download", "backup-20261101-020000.tar.gz", "-o", str(out)],
                tmp_path,
                make_cfg(),
                MagicMock(),
            )
        assert result.exit_code == 0, result.output
        assert "secrets are not in this backup" in plain(result.output)

    def test_hostile_names_in_a_downloaded_manifest_are_not_printed(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.download.return_value = _archive(
            {"secret_names": ["GOOD", "evil\nPASSWORD=hunter2"], "secrets_included": False}
        )
        out = tmp_path / "b.tar.gz"
        with patch("dango.platform.cloud.spaces.SpacesClient", return_value=client):
            result = invoke(
                remote,
                ["backup", "download", "backup-20261101-020000.tar.gz", "-o", str(out)],
                tmp_path,
                make_cfg(),
                MagicMock(),
            )
        assert "GOOD" in result.output
        assert "hunter2" not in result.output

    def test_download_from_server_names_what_to_re_enter(self, tmp_path: Path) -> None:
        payload = {
            "path": "/srv/dango/backups/deploy/backup-20261101-020000.tar.gz",
            "warnings": [],
            "secret_names": ["API_KEY", "sources.s.client_secret"],
            "secrets_included": False,
        }
        ssh = make_ssh(run_stdout="__BACKUP_RESULT__" + json.dumps(payload) + "\n")
        out = tmp_path / "b.tar.gz"
        ssh.download_file.side_effect = lambda _remote, local: Path(local).write_bytes(b"x")
        result = invoke(
            remote,
            ["backup", "download", "--from-server", "-o", str(out)],
            tmp_path,
            make_cfg(),
            ssh,
        )
        assert result.exit_code == 0, result.output
        assert _REENTER in plain(result.output)

    def test_restore_from_spaces_names_what_to_re_enter(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup_destination import RESTORE_SECRETS_PREFIX

        marker = RESTORE_SECRETS_PREFIX + json.dumps(["API_KEY", "sources.s.client_secret"]) + "\n"
        result = invoke(
            remote,
            ["backup", "restore", "backup-20261101-020000.tar.gz", "-y"],
            tmp_path,
            make_cfg(),
            make_ssh(run_stdout=marker),
        )
        assert result.exit_code == 0, result.output
        assert _REENTER in plain(result.output)

    def test_restore_from_a_local_file_names_what_to_re_enter(self, tmp_path: Path) -> None:
        archive = tmp_path / "mine.tar.gz"
        archive.write_bytes(_archive(_MANIFEST))
        restored = MagicMock(health_check_passed=True, warnings=[])
        with (
            patch("dango.platform.cloud.backup.make_private_dir", return_value="/root/x"),
            patch("dango.platform.cloud.backup.restore_from_archive", return_value=restored),
        ):
            result = invoke(
                remote,
                ["backup", "restore", str(archive), "--from-local", "-y"],
                tmp_path,
                make_cfg(),
                make_ssh(),
            )
        assert result.exit_code == 0, result.output
        assert _REENTER in plain(result.output)

    def test_a_backup_with_secrets_does_not_say_they_are_missing(self, tmp_path: Path) -> None:
        client = MagicMock()
        client.download.return_value = _archive({"secret_names": ["A"], "secrets_included": True})
        out = tmp_path / "b.tar.gz"
        with patch("dango.platform.cloud.spaces.SpacesClient", return_value=client):
            result = invoke(
                remote,
                ["backup", "download", "backup-20261101-020000.tar.gz", "-o", str(out)],
                tmp_path,
                make_cfg(),
                MagicMock(),
            )
        assert "secrets are not in this backup" not in plain(result.output)


# ---------------------------------------------------------------------------
# Reminder before destructive commands
# ---------------------------------------------------------------------------

_SERVER_ENV = "API_KEY=value-aaa-111\nDB_PASSWORD=value-bbb-222\n"
_SERVER_TOML = '[sources.s]\nclient_secret = "value-ccc-333"\n'


def _assert_reminder(text: str, *, byos: bool) -> None:
    flat = plain(text)
    assert ".env" in flat and ".dlt/secrets.toml" in flat
    assert "NOT in backups" in flat
    for name in ("API_KEY", "DB_PASSWORD", "sources.s.client_secret"):
        assert name in flat
    assert "value-aaa-111" not in flat
    assert "value-bbb-222" not in flat
    assert "value-ccc-333" not in flat
    if byos:
        assert "lost with the server" in flat or "lost with it" in flat
        assert DOWNLOAD_HINT in flat


@pytest.mark.unit
class TestDestructiveCommandsRemindAboutSecrets:
    def _confirm_no(self) -> dict[str, Any]:
        return {"input": "n\n"}

    def test_restore_from_spaces_reminds_before_asking(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path=tmp_path)
        ssh = make_ssh(env_text=_SERVER_ENV, toml_text=_SERVER_TOML)
        result = invoke(
            remote,
            ["backup", "restore", "backup-20261101-020000.tar.gz"],
            tmp_path,
            cfg,
            ssh,
            input="n\n",
        )
        assert "cancelled" in result.output.lower()
        _assert_reminder(result.output, byos=False)

    def test_restore_from_a_local_file_reminds_before_asking(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path=tmp_path)
        archive = tmp_path / "mine.tar.gz"
        archive.write_bytes(_archive(_MANIFEST))
        ssh = make_ssh(env_text=_SERVER_ENV, toml_text=_SERVER_TOML)
        result = invoke(
            remote,
            ["backup", "restore", str(archive), "--from-local"],
            tmp_path,
            cfg,
            ssh,
            input="n\n",
        )
        assert "cancelled" in result.output.lower()
        _assert_reminder(result.output, byos=False)

    def test_resize_reminds_before_asking(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path=tmp_path)
        ssh = make_ssh(env_text=_SERVER_ENV, toml_text=_SERVER_TOML)
        result = invoke(remote, ["resize", "s-4vcpu-8gb"], tmp_path, cfg, ssh, input="n\n")
        assert "Resize plan" in result.output
        _assert_reminder(result.output, byos=False)

    def test_destroy_do_reminds_even_with_force(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path=tmp_path)
        ssh = make_ssh(env_text=_SERVER_ENV, toml_text=_SERVER_TOML)
        with (
            patch(CTX, return_value=tmp_path),
            patch(LOADER, return_value=make_loader(cfg)),
            patch(SSH, return_value=ssh),
            patch.dict("os.environ", {"DIGITALOCEAN_TOKEN": "t"}),
            patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=MagicMock()),
        ):
            result = CliRunner().invoke(
                deploy,
                ["destroy", "--force"],
                obj={"project_root": tmp_path},
                catch_exceptions=True,
            )
        _assert_reminder(result.output, byos=False)

    def test_destroy_byos_reminds_and_says_backups_die_with_the_server(
        self, tmp_path: Path
    ) -> None:
        cfg = make_cfg(spaces=False, provider="byos", tmp_path=tmp_path)
        ssh = make_ssh(env_text=_SERVER_ENV, toml_text=_SERVER_TOML)
        with (
            patch(CTX, return_value=tmp_path),
            patch(LOADER, return_value=make_loader(cfg)),
            patch(SSH, return_value=ssh),
        ):
            result = CliRunner().invoke(
                deploy,
                ["destroy"],
                obj={"project_root": tmp_path},
                input="wrong\n",
                catch_exceptions=True,
            )
        assert "Aborted" in result.output
        _assert_reminder(result.output, byos=True)

    def test_an_unreachable_server_still_gets_the_plain_reminder(self, tmp_path: Path) -> None:
        cfg = make_cfg(tmp_path=tmp_path)
        ssh = MagicMock()
        ssh.connect.side_effect = OSError("no route")
        result = invoke(
            remote,
            ["backup", "restore", "backup-20261101-020000.tar.gz"],
            tmp_path,
            cfg,
            ssh,
            input="n\n",
        )
        flat = plain(result.output)
        assert "NOT in backups" in flat
        assert "cancelled" in flat.lower()
