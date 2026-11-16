"""tests/unit/test_remote_status_docker_access.py

`remote status` shows how the server reaches Docker and the recorded server version, and
warns when the local client is older than that version. Parsing is tolerant of garbage.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.platform.cloud import server_status as ss
from dango.platform.cloud.server_status import ServerStatus, collect_server_status
from tests.unit.test_remote_management_cli import _make_loader
from tests.unit.test_server_status import _make_cloud_config, _make_result, _make_ssh

_MARK = "server-version.json"


def _probe(marker="1", groups="dango www-data", record='{"dango": "1.0.14"}') -> str:
    lines = [f"marker={marker}"]
    if groups is not None:
        lines.append(f"groups={groups}")
    lines += ["---", record]
    return "\n".join(lines) + "\n"


def _collect(stdout: str, exit_code: int = 0) -> ServerStatus:
    ssh = _make_ssh({_MARK: _make_result(stdout=stdout, exit_code=exit_code)})
    return collect_server_status(ssh, _make_cloud_config())


@pytest.mark.unit
class TestDockerAccessParsing:
    @pytest.mark.parametrize(
        ("marker", "groups", "expected"),
        [
            ("1", "dango www-data", "helper"),
            ("0", "dango docker", "group (legacy)"),
            ("1", "dango docker", "migrating"),
            ("0", "dango", "none"),
        ],
    )
    def test_states(self, marker: str, groups: str, expected: str) -> None:
        assert _collect(_probe(marker, groups)).docker_access == expected

    def test_group_with_server_record_of_fallback_is_fallback(self) -> None:
        record = '{"dango": "1.0.14", "docker_access": "group"}'
        assert _collect(_probe("0", "dango docker", record)).docker_access == "group (fallback)"

    def test_group_without_record_is_legacy(self) -> None:
        assert _collect(_probe("0", "dango docker", "")).docker_access == "group (legacy)"

    def test_record_claiming_helper_does_not_hide_a_group_member(self) -> None:
        record = '{"dango": "1.0.14", "docker_access": "helper"}'
        assert _collect(_probe("0", "dango docker", record)).docker_access == "group (legacy)"

    def test_group_match_is_whole_word(self) -> None:
        assert _collect(_probe("0", "dango dockerish")).docker_access == "none"

    @pytest.mark.parametrize(
        "stdout",
        ["", "\x00\xff garbage", "marker=maybe\ngroups=docker\n", "groups=dango\n", "marker=1\n"],
    )
    def test_garbled_output_is_unknown(self, stdout: str) -> None:
        status = _collect(stdout)
        assert status.docker_access == "unknown"

    def test_failed_command_is_unknown(self) -> None:
        status = _collect(_probe(), exit_code=1)
        assert status.docker_access == "unknown"
        assert status.server_recorded_version is None

    def test_silent_remote_is_unknown(self) -> None:
        assert ss._get_docker_access(_make_ssh()) == ("unknown", None)


@pytest.mark.unit
class TestRecordedVersion:
    def test_valid_record(self) -> None:
        assert _collect(_probe()).server_recorded_version == "1.0.14"

    @pytest.mark.parametrize(
        "record",
        [
            "",
            "not json",
            "[]",
            '{"dango": 5}',
            '{"dango": "/srv/dango/project/.env"}',
            '{"dango": "1.0.14; rm -rf /"}',
            '{"dango": "' + "9" * 500 + '"}',
            '{"other": "1.0.14"}',
            "[" * 4096,
            '{"dango": "\\u001b[31m1.0.14"}',
            '{"dango": "1.0.14\\u0000"}',
            '{"dango": "1.0.14\\n"}',
            '{"dango": "\\u202e1.0.14"}',
        ],
    )
    def test_bad_record_is_unknown_and_never_echoed(self, record: str) -> None:
        status = _collect(_probe(record=record))
        assert status.server_recorded_version is None
        assert status.docker_access == "helper"

    def test_only_the_version_is_extracted(self) -> None:
        record = json.dumps({"dango": "1.0.14", "token": "s3cret", "path": "/root/x"})
        status = _collect(_probe(record=record))
        assert status.server_recorded_version == "1.0.14"
        assert "s3cret" not in repr(status)
        assert "/root/x" not in repr(status)


@pytest.mark.unit
class TestClientWarning:
    @pytest.mark.parametrize(
        ("server", "local", "warns"),
        [
            ("1.0.14", "1.0.13", True),
            ("1.0.14", "1.0.14", False),
            ("1.0.14", "1.1.0", False),
            ("1.0.14", "1.0.14rc1", True),
            (None, "1.0.13", False),
            ("1.0.14", "garbage", False),
        ],
    )
    def test_warning(self, server: str | None, local: str, warns: bool) -> None:
        message = ss.client_version_warning(server, local)
        assert (message is not None) is warns
        if message:
            assert "pip install -U getdango" in message
            assert server in message and local in message


def _run_status(tmp_path: Path, **status_kwargs: object):
    defaults = {"dango_version": "1.0.14", "docker_access": "helper"}
    defaults.update(status_kwargs)
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.config.loader.ConfigLoader", return_value=_make_loader()),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
        patch(
            "dango.platform.cloud.server_status.collect_server_status",
            return_value=ServerStatus(**defaults),
        ),
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=None),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.13"),
    ):
        return CliRunner().invoke(remote, ["status"], obj={"project_root": tmp_path})


@pytest.mark.unit
class TestStatusDisplay:
    @pytest.mark.parametrize(
        "state", ["helper", "group (fallback)", "migrating", "none", "unknown"]
    )
    def test_shows_docker_access(self, tmp_path: Path, state: str) -> None:
        result = _run_status(tmp_path, docker_access=state)
        assert result.exit_code == 0, result.output
        assert "Docker access" in result.output
        assert state in result.output

    def test_shows_recorded_version(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, server_recorded_version="1.0.14")
        assert "Recorded: 1.0.14" in result.output

    def test_missing_record_shows_unknown(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, server_recorded_version=None)
        assert "Recorded: unknown" in result.output

    def test_old_client_warning(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, server_recorded_version="1.0.14")
        assert "pip install -U getdango" in result.output

    def test_no_warning_without_record(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, server_recorded_version=None)
        assert "pip install -U getdango" not in result.output

    def test_fallback_prints_warning(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, docker_access="group (fallback)")
        assert "dango remote upgrade" in " ".join(result.output.split())
        assert "group access" in " ".join(result.output.split())

    def test_none_prints_repair_error(self, tmp_path: Path) -> None:
        result = _run_status(tmp_path, docker_access="none")
        assert "cannot reach Docker" in " ".join(result.output.split())

    @pytest.mark.parametrize("state", ["helper", "group (legacy)", "unknown"])
    def test_no_warning_for_other_states(self, tmp_path: Path, state: str) -> None:
        result = _run_status(tmp_path, docker_access=state)
        text = " ".join(result.output.split())
        assert "group access" not in text
        assert "cannot reach Docker" not in text


@pytest.mark.unit
class TestFailedProbe:
    def test_ssh_error_in_probe_is_unknown(self) -> None:
        from dango.exceptions import CloudSSHError

        ssh = MagicMock()
        ssh.exec_command.side_effect = CloudSSHError("timed out")
        assert ss._get_docker_access(ssh) == ("unknown", None)

    def test_status_command_survives_a_failed_collect(self, tmp_path: Path) -> None:
        from dango.exceptions import CloudSSHError

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.loader.ConfigLoader", return_value=_make_loader()),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
            patch(
                "dango.platform.cloud.server_status.collect_server_status",
                side_effect=CloudSSHError("timed out"),
            ),
            patch("dango.platform.cloud.server_status.check_latest_pypi_version"),
        ):
            result = CliRunner().invoke(remote, ["status"], obj={"project_root": tmp_path})
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)


def _run_probe(tmp_path: Path, uid: int, *, as_root_uid: int | None = None) -> str:
    """Run the production probe text against a temp tree standing in for /srv/dango."""
    text = ss._DOCKER_ACCESS_CMD
    swaps = {
        "ROOT=0;": f"ROOT={uid if as_root_uid is None else as_root_uid};",
        "S=/srv/dango;": f"S={tmp_path};",
        "id -nG dango": "echo dango www-data",
    }
    for old, new in swaps.items():
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    out = subprocess.run(
        ["sh", "-c", text], capture_output=True, text=True, timeout=20, check=False
    )
    return out.stdout


def _tree(tmp_path: Path, record: str = '{"dango": "1.0.14"}') -> Path:
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "active").write_text("")
    (tmp_path / "compose" / "active").chmod(0o644)
    version = tmp_path / "server-version.json"
    version.write_text(record)
    version.chmod(0o644)
    tmp_path.chmod(0o755)
    return version


@pytest.mark.unit
class TestGeneratedProbe:
    def test_good_tree(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        out = _run_probe(tmp_path, os.getuid())
        assert ss._parse_docker_access(out) == ("helper", "1.0.14")

    def test_fifo_is_not_read_and_does_not_hang(self, tmp_path: Path) -> None:
        version = _tree(tmp_path)
        version.unlink()
        os.mkfifo(version)
        out = _run_probe(tmp_path, os.getuid())
        assert ss._parse_docker_access(out) == ("helper", None)

    def test_symlink_is_not_followed(self, tmp_path: Path) -> None:
        version = _tree(tmp_path)
        version.unlink()
        target = tmp_path / "elsewhere"
        target.write_text('{"dango": "9.9.9"}')
        version.symlink_to(target)
        assert ss._parse_docker_access(_run_probe(tmp_path, os.getuid()))[1] is None

    def test_foreign_owner_is_not_read(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        out = _run_probe(tmp_path, os.getuid(), as_root_uid=os.getuid() + 1)
        assert ss._parse_docker_access(out) == ("none", None)

    @pytest.mark.parametrize("mode", [0o664, 0o646])
    def test_writable_record_is_not_read(self, tmp_path: Path, mode: int) -> None:
        _tree(tmp_path).chmod(mode)
        assert ss._parse_docker_access(_run_probe(tmp_path, os.getuid()))[1] is None

    @pytest.mark.parametrize("mode", [0o775, 0o757])
    def test_writable_server_dir_blocks_the_read(self, tmp_path: Path, mode: int) -> None:
        _tree(tmp_path)
        tmp_path.chmod(mode)
        assert ss._parse_docker_access(_run_probe(tmp_path, os.getuid()))[1] is None

    @pytest.mark.parametrize("mode", [0o664, 0o646])
    def test_writable_marker_is_not_trusted(self, tmp_path: Path, mode: int) -> None:
        _tree(tmp_path)
        (tmp_path / "compose" / "active").chmod(mode)
        assert "marker=0" in _run_probe(tmp_path, os.getuid())

    def test_symlinked_marker_is_not_trusted(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        marker = tmp_path / "compose" / "active"
        marker.unlink()
        real = tmp_path / "real"
        real.write_text("")
        real.chmod(0o644)
        marker.symlink_to(real)
        assert "marker=0" in _run_probe(tmp_path, os.getuid())

    def test_read_is_wrapped_in_a_timeout(self) -> None:
        assert "timeout 5 head -c" in ss._DOCKER_ACCESS_CMD
