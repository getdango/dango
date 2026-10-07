"""tests/unit/test_remote_compose_project_name.py

Tests that get_remote_compose_project_name only accepts an 8-hex project-id prefix
and otherwise falls back to the legacy path hash, since the name reaches remote shells.
"""

import re
import types
import uuid
from unittest.mock import patch

import pytest

from dango.platform.cloud.backup import PROJECT_DIR, get_remote_compose_project_name
from dango.platform.docker import _legacy_path_hash

_SAFE = re.compile(r"^dango-[a-f0-9]{8}$")
_LEGACY = f"dango-{_legacy_path_hash(PROJECT_DIR)}"


def _ssh(yaml_text: str):
    return types.SimpleNamespace(
        exec_command=lambda cmd, timeout=10: types.SimpleNamespace(success=True, stdout=yaml_text)
    )


def _ssh_id(value: str):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return _ssh(f'project:\n  id: "{escaped}"\n')


@pytest.mark.unit
class TestRemoteComposeProjectName:
    def test_valid_remote_id_gives_compose_name(self):
        pid = uuid.uuid4().hex
        assert get_remote_compose_project_name(_ssh_id(pid)) == f"dango-{pid[:8]}"

    @pytest.mark.parametrize(
        "bad",
        ["x; rm -rf /", "$(id)abcde", "`id`abcdef", "../../etc", "ABCDEF12", "abc", "abcdefg h"],
    )
    def test_hostile_remote_ids_fall_back_to_legacy_hash(self, bad):
        name = get_remote_compose_project_name(_ssh_id(bad))
        assert _SAFE.match(name)
        assert name == _LEGACY

    @pytest.mark.parametrize(
        "pid,expected",
        [("0123456789;rm -rf /", "dango-01234567"), ("aaaaaaaa\n", "dango-aaaaaaaa")],
    )
    def test_only_first_eight_characters_matter(self, pid, expected):
        assert get_remote_compose_project_name(_ssh_id(pid)) == expected

    @pytest.mark.parametrize(
        "yaml_text",
        [
            "project:\n  id: 123\n",
            'project:\n  id: ["a"]\n',
            'project:\n  id: ""\n',
            "project:\n  name: x\n",
        ],
    )
    def test_missing_or_non_string_id_falls_back(self, yaml_text):
        assert get_remote_compose_project_name(_ssh(yaml_text)) == _LEGACY

    def test_ssh_failure_falls_back(self):
        failed = types.SimpleNamespace(
            exec_command=lambda cmd, timeout=10: types.SimpleNamespace(success=False, stdout="")
        )

        def _raise(cmd, timeout=10):
            raise RuntimeError("ssh down")

        raising = types.SimpleNamespace(exec_command=_raise)
        assert get_remote_compose_project_name(failed) == _LEGACY
        assert get_remote_compose_project_name(raising) == _LEGACY

    def test_rejection_logs_without_raw_id(self):
        bad = "x; rm -rf /"
        with patch("dango.platform.cloud.backup._logger") as log:
            get_remote_compose_project_name(_ssh_id(bad))
        log.warning.assert_called_once()
        assert "remote_project_id_rejected" == log.warning.call_args.args[0]
        assert "x; rm" not in repr(log.warning.call_args)
