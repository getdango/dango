"""tests/unit/test_remote_upload.py

Tests for ``dango remote upload``: target resolution and the part-file upload sequence.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.platform.cloud.ssh import CommandResult

_DATA = "/srv/dango/project/data"


def _resolve(local: Path, remote: str | None, root: Path) -> str:
    from dango.cli.commands.remote_upload import resolve_remote_target

    return resolve_remote_target(local, remote, root)


@pytest.mark.unit
class TestResolveRemoteTarget:
    def test_default_is_path_relative_to_project_root(self, tmp_path):
        assert _resolve(tmp_path / "data" / "uploads" / "a.csv", None, tmp_path) == (
            f"{_DATA}/uploads/a.csv"
        )

    def test_explicit_remote_path(self, tmp_path):
        local = tmp_path / "x.csv"
        assert _resolve(local, "data/seeds/x.csv", tmp_path) == f"{_DATA}/seeds/x.csv"

    @pytest.mark.parametrize(
        "remote",
        [
            "../x",
            "/etc/passwd",
            "data/../.env",
            "data/uploads/../../.env",
            "dbt/models/x.sql",
            "data/warehouse.duckdb",
            "data/warehouse.duckdb.wal",
            "data/.tmp/x",
            "data/.tmp",
            "data",
            "data/",
            "data/a\x00.csv",
            "",
        ],
    )
    def test_bad_remote_path_is_rejected(self, tmp_path, remote):
        with pytest.raises(click.UsageError):
            _resolve(tmp_path / "x.csv", remote, tmp_path)

    @pytest.mark.parametrize("local", ["dbt/models/x.sql", "x.csv", "../outside/data/x.csv"])
    def test_default_outside_data_is_rejected(self, tmp_path, local):
        with pytest.raises(click.UsageError, match="only files under data/"):
            _resolve(tmp_path / local, None, tmp_path)

    def test_default_for_warehouse_is_rejected(self, tmp_path):
        with pytest.raises(click.UsageError):
            _resolve(tmp_path / "data" / "warehouse.duckdb", None, tmp_path)


class _FakeSSH:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.disconnected = False

    def exec_command(self, command: str, timeout=None, check: bool = False) -> CommandResult:
        self.events.append(("exec", command))
        return CommandResult(stdout="", stderr="", exit_code=0)

    def upload_file(self, local, remote, callback=None, timeout=None) -> None:
        self.events.append(("upload", remote))

    def disconnect(self) -> None:
        self.disconnected = True


def _invoke(tmp_path: Path, args: list[str], ssh: _FakeSSH | None = None):
    from dango.cli.commands.remote import remote

    ssh = ssh or _FakeSSH()
    with patch(
        "dango.cli.commands.remote._ssh_connect_or_fail",
        return_value=(MagicMock(), ssh, tmp_path),
    ):
        result = CliRunner().invoke(remote, ["upload", *args], obj={})
    return result, ssh


@pytest.mark.unit
class TestUploadCommand:
    def test_upload_uses_part_file_then_rename(self, tmp_path):
        f = tmp_path / "data" / "uploads" / "a.csv"
        f.parent.mkdir(parents=True)
        f.write_text("a,b\n1,2\n")
        result, ssh = _invoke(tmp_path, [str(f)])
        assert result.exit_code == 0, result.output
        kinds = [k for k, _ in ssh.events]
        assert kinds == ["exec", "upload", "exec"]
        assert ssh.events[0][1].startswith("mkdir -p ")
        assert ssh.events[1][1] == f"{_DATA}/uploads/a.csv.part"
        assert "mv -fT" in ssh.events[2][1]
        assert f"{_DATA}/uploads/a.csv.part" in ssh.events[2][1]
        assert ssh.events[2][1].rstrip().endswith(f"{_DATA}/uploads/a.csv")
        assert f"{_DATA}/uploads/a.csv" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert ssh.disconnected

    def test_upload_rejects_symlink_and_directory(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        real = data / "real.csv"
        real.write_text("x")
        link = data / "link.csv"
        link.symlink_to(real)
        folder = data / "folder"
        folder.mkdir()
        for target in (link, folder):
            result, ssh = _invoke(tmp_path, [str(target)])
            assert result.exit_code != 0
            assert ssh.events == []

    def test_upload_rejects_missing_file(self, tmp_path):
        (tmp_path / "data").mkdir()
        result, ssh = _invoke(tmp_path, [str(tmp_path / "data" / "nope.csv")])
        assert result.exit_code != 0
        assert ssh.events == []

    def test_upload_enforces_size_cap(self, tmp_path):
        f = tmp_path / "data" / "big.csv"
        f.parent.mkdir()
        f.write_text("x")
        with patch("dango.cli.commands.remote_upload.MAX_UPLOAD_BYTES", 0):
            result, ssh = _invoke(tmp_path, [str(f)])
        assert result.exit_code != 0
        assert "2 GiB" in result.output or "limit" in result.output.lower()
        assert ssh.events == []

    def test_failed_rename_removes_part_file(self, tmp_path):
        f = tmp_path / "data" / "a.csv"
        f.parent.mkdir()
        f.write_text("x")

        class _Failing(_FakeSSH):
            def exec_command(self, command, timeout=None, check=False):
                self.events.append(("exec", command))
                code = 1 if "mv -fT" in command else 0
                return CommandResult(stdout="", stderr="boom", exit_code=code)

        result, ssh = _invoke(tmp_path, [str(f)], _Failing())
        assert result.exit_code != 0
        assert any("rm -f" in c and ".part" in c for k, c in ssh.events if k == "exec")
        assert ssh.disconnected

    def test_uploading_outside_data_with_explicit_remote_is_rejected(self, tmp_path):
        f = tmp_path / "x.csv"
        f.write_text("x")
        result, ssh = _invoke(tmp_path, [str(f), "dbt/seeds/x.csv"])
        assert result.exit_code != 0
        assert ssh.events == []
