"""tests/unit/test_remote_status_docker_access.py

`remote status` shows how the server reaches Docker and the recorded server version, and
warns when the local client is older than that version. Parsing is tolerant of garbage.
"""

from __future__ import annotations

import json
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
            ("0", "dango docker", "group (fallback)"),
            ("1", "dango docker", "migrating"),
            ("0", "dango", "none"),
        ],
    )
    def test_states(self, marker: str, groups: str, expected: str) -> None:
        assert _collect(_probe(marker, groups)).docker_access == expected

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
        ],
    )
    def test_bad_record_is_unknown_and_never_echoed(self, record: str) -> None:
        status = _collect(_probe(record=record))
        assert status.server_recorded_version is None
        assert status.docker_access == "helper"

    def test_only_the_version_is_extracted(self) -> None:
        record = json.dumps({"dango": "1.0.14", "token": "s3cret", "path": "/root/x"})
        status = _collect(_probe(record=record))
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


def _run_status(**status_kwargs: object):
    defaults = {"dango_version": "1.0.14", "docker_access": "helper"}
    defaults.update(status_kwargs)
    with (
        patch("dango.cli.utils.require_project_context", return_value=None),
        patch("dango.config.loader.ConfigLoader", return_value=_make_loader()),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
        patch(
            "dango.platform.cloud.server_status.collect_server_status",
            return_value=ServerStatus(**defaults),
        ),
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=None),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.13"),
    ):
        return CliRunner().invoke(remote, ["status"], obj={"project_root": None})


@pytest.mark.unit
class TestStatusDisplay:
    @pytest.mark.parametrize(
        "state", ["helper", "group (fallback)", "migrating", "none", "unknown"]
    )
    def test_shows_docker_access(self, state: str) -> None:
        result = _run_status(docker_access=state)
        assert result.exit_code == 0, result.output
        assert "Docker access" in result.output
        assert state in result.output

    def test_shows_recorded_version(self) -> None:
        result = _run_status(server_recorded_version="1.0.14")
        assert "Recorded: 1.0.14" in result.output

    def test_missing_record_shows_unknown(self) -> None:
        result = _run_status(server_recorded_version=None)
        assert "Recorded: unknown" in result.output

    def test_old_client_warning(self) -> None:
        result = _run_status(server_recorded_version="1.0.14")
        assert "pip install -U getdango" in result.output

    def test_no_warning_without_record(self) -> None:
        result = _run_status(server_recorded_version=None)
        assert "pip install -U getdango" not in result.output
