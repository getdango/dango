"""tests/unit/test_backup_destination_review.py

Review fixes for the backup destination: cloud.yml read as data by root, off-site claims that need a server able to upload, secret names that never hold value fragments, and old archives that held secrets.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from tests.factories.backup_cli import (
    DOWNLOAD_HINT,
    ENV_WITH_KEYS,
    invoke,
    make_cfg,
    make_ssh,
    plain,
)

_SB = "dango.platform.cloud.scheduled_backup"
_SECRET = "TOPSECRET-sk-live-123"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "srv-project"
    (project / ".dango").mkdir(parents=True)
    return project


# ---------------------------------------------------------------------------
# H1: cloud.yml is data root reads without following links or blocking
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCloudYmlReadAsData:
    def test_a_symlinked_cloud_yml_is_not_read(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_backup_config, _load_spaces_config

        project = _project(tmp_path)
        root_only = tmp_path / "root-only.yml"
        root_only.write_text(
            "spaces:\n  bucket: root-only-bucket\nbackup:\n  include_secrets: true\n"
        )
        (project / ".dango" / "cloud.yml").symlink_to(root_only)
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError):
                _load_spaces_config()
            assert _load_backup_config() == {}

    def test_a_symlinked_dango_directory_is_not_followed(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_backup_config, _load_spaces_config

        project = tmp_path / "srv-project"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "cloud.yml").write_text("spaces:\n  bucket: root-only-bucket\n")
        (project / ".dango").symlink_to(elsewhere)
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError):
                _load_spaces_config()
            assert _load_backup_config() == {}

    def test_a_fifo_does_not_block_root(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_backup_config, _load_spaces_config

        project = _project(tmp_path)
        os.mkfifo(project / ".dango" / "cloud.yml")
        outcome: list[object] = []

        def run() -> None:
            with patch(f"{_SB}.PROJECT_DIR", project):
                outcome.append(_load_backup_config())
                try:
                    _load_spaces_config()
                except CloudProvisioningError as exc:
                    outcome.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "reading cloud.yml blocked on a FIFO"
        assert outcome[0] == {}

    def test_a_yaml_error_carries_none_of_the_file(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_backup_config, _load_spaces_config

        project = _project(tmp_path)
        (project / ".dango" / "cloud.yml").write_text(f"db_password: [unclosed {_SECRET}\n")
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError) as info:
                _load_spaces_config()
            assert _SECRET not in str(info.value)
            assert _load_backup_config() == {}

    def test_a_run_with_a_bad_cloud_yml_leaks_nothing_to_the_result_or_health_file(
        self, tmp_path: Path
    ) -> None:
        from dango.platform.cloud.scheduled_backup import run_scheduled_backup

        project = _project(tmp_path)
        (project / ".dango" / "cloud.yml").write_text(f"db_password: [unclosed {_SECRET}\n")
        backups = tmp_path / "bk"
        backups.mkdir()
        archive = backups / "backup-20261101-020000.tar.gz"
        archive.write_bytes(b"x")
        health = tmp_path / "health.json"
        with (
            patch(f"{_SB}.PROJECT_DIR", project),
            patch(f"{_SB}.BACKUP_DIR", backups),
            patch(f"{_SB}.HEALTH_FILE", health),
            patch(f"{_SB}.MARKER_FILE", tmp_path / "marker"),
            patch(f"{_SB}._acquire_backup_lock", return_value=MagicMock()),
            patch(f"{_SB}.fcntl.flock"),
            patch(f"{_SB}._create_local_archive", return_value=(archive, {}, [])),
        ):
            result = run_scheduled_backup()
        assert _SECRET not in json.dumps([result.error, result.warnings])
        assert _SECRET not in (health.read_text() if health.exists() else "")

    def test_a_cloud_yml_that_is_not_a_mapping_counts_as_empty(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_backup_config

        project = _project(tmp_path)
        (project / ".dango" / "cloud.yml").write_text("- just\n- a list\n")
        with patch(f"{_SB}.PROJECT_DIR", project):
            assert _load_backup_config() == {}


# ---------------------------------------------------------------------------
# M1: "Off-site: yes" needs a server that reads the destination, and a timer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServerReadsDestination:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("1.0.14", True),
            ("1.1.0", True),
            ("1.0.13", False),
            ("", False),
            (None, False),
            ("x", False),
        ],
    )
    def test_version_rule(self, version: str | None, expected: bool) -> None:
        from dango.platform.cloud.backup_destination import server_reads_destination

        assert server_reads_destination(version) is expected


_ENV = ENV_WITH_KEYS + "SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"


@pytest.mark.unit
class TestOffsiteNeedsAnUploadingServer:
    def test_enable_on_an_older_server_is_not_yes(self, tmp_path: Path) -> None:
        from dango.cli.commands.remote import remote

        ssh = make_ssh(env_text=_ENV, version="1.0.13")
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        text = plain(result.output)
        assert result.exit_code == 0, result.output
        assert "Off-site: yes" not in text
        assert "Off-site: unknown" in text
        assert "dango remote upgrade" in text
        assert DOWNLOAD_HINT in text

    def test_enable_with_an_unreadable_version_is_not_yes(self, tmp_path: Path) -> None:
        from dango.cli.commands.remote import remote

        ssh = make_ssh(env_text=_ENV, version="not-a-version")
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        assert "Off-site: yes" not in plain(result.output)

    def test_status_on_an_older_server_is_not_yes(self) -> None:
        from dango.cli.commands.backup_notices import backup_status_lines

        lines = plain(" ".join(backup_status_lines(make_ssh(env_text=_ENV, version="1.0.13"))))
        assert "Off-site: yes" not in lines
        assert "Off-site: unknown" in lines

    def test_status_with_the_timer_off_is_not_yes(self) -> None:
        from dango.cli.commands.backup_notices import backup_status_lines

        lines = plain(" ".join(backup_status_lines(make_ssh(env_text=_ENV, timer="inactive"))))
        assert "Off-site: yes" not in lines
        assert "Off-site: NO" in lines
        assert "not enabled" in lines
        assert "my-bucket" in lines

    def test_status_with_everything_in_place_is_yes(self) -> None:
        from dango.cli.commands.backup_notices import backup_status_lines

        lines = plain(" ".join(backup_status_lines(make_ssh(env_text=_ENV))))
        assert "Off-site: yes (Spaces my-bucket)" in lines


# ---------------------------------------------------------------------------
# L1: names never hold value fragments
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNamesNeverHoldValueFragments:
    def test_dangos_own_serializer_output(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names
        from dango.utils.env_file import serialize_env_file

        text = serialize_env_file({"K": 'pass"\nLEAKED_VALUE_PART=xyz', "B": "1"})
        assert env_secret_names(text) == ["B", "K"]

    @pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "])
    def test_unicode_and_control_line_breaks_inside_a_value(self, sep: str) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        assert env_secret_names(f"K=abc{sep}LEAKED=1\nB=2\n") == ["B", "K"]

    def test_an_escaped_quote_does_not_close_a_multiline_value(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        text = 'K="a\\"\nFAKE=1\nmore \\" still inside\nALSO_FAKE=2\nend"\nNEXT=3\n'
        assert env_secret_names(text) == ["K", "NEXT"]

    def test_crlf_files_still_list_names(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        assert env_secret_names("A=1\r\nB=2\r\n") == ["A", "B"]


# ---------------------------------------------------------------------------
# L2: an older archive that held secrets is not reported as without them
# ---------------------------------------------------------------------------


def _archive(manifest: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("backup-1/manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.unit
class TestOlderArchivesThatHeldSecrets:
    @pytest.mark.parametrize("secret", [".env", ".dlt/secrets.toml"])
    def test_inferred_from_the_file_list(self, secret: str) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        manifest = {"files": [{"path": ".dango/project.yml"}, {"path": secret}]}
        assert archive_secrets_info(_archive(manifest)) == (None, True)

    def test_an_older_archive_without_them_is_still_without(self) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        manifest = {"files": [{"path": ".dango/project.yml"}]}
        assert archive_secrets_info(_archive(manifest)) == (None, False)

    def test_the_explicit_flag_wins_over_the_file_list(self) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        manifest = {"files": [{"path": ".env"}], "secret_names": ["A"], "secrets_included": False}
        assert archive_secrets_info(_archive(manifest)) == (["A"], False)

    def test_download_of_such_an_archive_does_not_say_secrets_are_missing(
        self, tmp_path: Path
    ) -> None:
        from dango.cli.commands.remote import remote

        client = MagicMock()
        client.download.return_value = _archive({"files": [{"path": ".env"}]})
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
        assert "secrets are not in this backup" not in plain(result.output)
