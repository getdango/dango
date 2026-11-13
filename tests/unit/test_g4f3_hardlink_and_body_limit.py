"""tests/unit/test_g4f3_hardlink_and_body_limit.py

Root scripts over service-writable trees need fs.protected_hardlinks=1: the server preflight
and the runner verify step refuse a host without it. The Caddyfile caps request bodies just
above the upload limit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud._server_templates import build_caddyfile
from dango.platform.cloud.srv_preflight import EXIT_UNSAFE, check_srv_dango, preflight_script
from dango.platform.cloud.ssh import CommandResult

DASH = shutil.which("dash") or "/bin/sh"


def _sysctl(tmp_path: Path, value: str | None) -> str:
    path = tmp_path / "protected_hardlinks"
    if value is not None:
        path.write_text(f"{value}\n")
    return str(path)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([DASH, "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.unit
class TestPreflightScriptChecksHardlinkProtection:
    @staticmethod
    def _script(tmp_path: Path, value: str | None) -> str:
        paths = (str(tmp_path / "srv"), str(tmp_path / "srv" / "dango"))
        return preflight_script(paths, protected_hardlinks_file=_sysctl(tmp_path, value))

    def test_enabled_passes(self, tmp_path: Path) -> None:
        assert _run(self._script(tmp_path, "1")).returncode == 0

    @pytest.mark.parametrize("value", ["0", "2", "", "garbage", None])
    def test_anything_else_is_refused_with_its_own_status(
        self, tmp_path: Path, value: str | None
    ) -> None:
        result = _run(self._script(tmp_path, value))
        assert result.returncode == 4, (value, result.returncode, result.stderr)
        assert result.returncode != EXIT_UNSAFE

    def test_default_script_is_valid_shell(self) -> None:
        res = subprocess.run([DASH, "-n", "-c", preflight_script()], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert "/proc/sys/fs/protected_hardlinks" in preflight_script()


@pytest.mark.unit
def test_check_srv_dango_explains_a_missing_hardlink_protection() -> None:
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=4)
    with pytest.raises(CloudProvisioningError) as excinfo:
        check_srv_dango(ssh)
    text = str(excinfo.value)
    assert "fs.protected_hardlinks" in text
    assert "sysctl" in text
    assert "No change was made" in text or "before changing anything" in text


@pytest.mark.unit
class TestRunnerVerifyStep:
    def test_verify_script_checks_the_sysctl_and_is_valid_shell(self) -> None:
        text = access.verify_script()
        assert "protected_hardlinks" in text
        res = subprocess.run([DASH, "-n", "-c", text], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    @staticmethod
    def _fragment(sysctl_file: str) -> str:
        return (
            'bad=0; fail() { echo "verify: $*" >&2; bad=1; }\n'
            + access.hardlink_protection_check(sysctl_file)
            + '\nexit "$bad"\n'
        )

    def test_disabled_fails_with_a_clear_message(self, tmp_path: Path) -> None:
        result = _run(self._fragment(_sysctl(tmp_path, "0")))
        assert result.returncode == 1
        assert "fs.protected_hardlinks" in result.stderr

    @pytest.mark.parametrize("value", [None, "", "x"])
    def test_unreadable_or_unexpected_value_fails_closed(
        self, tmp_path: Path, value: str | None
    ) -> None:
        assert _run(self._fragment(_sysctl(tmp_path, value))).returncode == 1

    def test_enabled_passes(self, tmp_path: Path) -> None:
        result = _run(self._fragment(_sysctl(tmp_path, "1")))
        assert result.returncode == 0, result.stderr


_SIZE = re.compile(r"max_size\s+(\d+)\s*(MB|MiB)\b")


@pytest.mark.unit
@pytest.mark.parametrize("domain", [None, "dango.example"])
def test_caddyfile_caps_request_bodies_slightly_above_the_upload_cap(domain: str | None) -> None:
    from dango.web.routes.upload import MAX_UPLOAD_BYTES

    text = build_caddyfile(domain=domain)
    block = re.search(r"\trequest_body \{\n\t\tmax_size [^\n]+\n\t\}", text)
    assert block, text
    match = _SIZE.search(block.group(0))
    assert match
    limit = int(match.group(1)) * (1_000_000 if match.group(2) == "MB" else 1_048_576)
    assert MAX_UPLOAD_BYTES < limit <= MAX_UPLOAD_BYTES * 1.25
    # a site-level directive, ahead of the handles that proxy to the app
    assert block.start() < text.index("\thandle ")
    assert text.count("{") == text.count("}")
