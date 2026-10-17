"""tests/unit/test_runtime_ownership_review.py

Root never runs or re-owns an untrusted venv; verification, guards and requirement rules hold.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError, CloudProvisioningError
from tests.unit.test_runtime_ownership import _recording_ssh

_VENV_EXEC = re.compile(r"/srv/dango/venv/bin/(python3?|pip|dango)\b")
_NEW_VENV = "python3 -m venv /srv/dango/venv"


def _old_venv_runs(commands: list[str]) -> list[str]:
    """Commands that run the venv as root while it is (still or again) the old one."""
    out: list[str] = []
    fresh = False
    for c in commands:
        if _NEW_VENV in c:
            fresh = True
        if re.search(r"mv /srv/dango/venv\.untrusted-\S+ /srv/dango/venv", c):
            fresh = False  # the old venv was put back
        if not fresh and _VENV_EXEC.search(c) and not re.search(r"sudo\s+-u\s+dango", c):
            out.append(c)
    return out


def _upgrade(ssh: MagicMock, latest: str = "1.0.15", **kwargs: Any) -> Any:
    from dango.platform.cloud.upgrade import upgrade_dango

    with (
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=latest),
        patch(
            "dango.platform.cloud.backup.create_backup", return_value=MagicMock(archive_path="a")
        ),
    ):
        return upgrade_dango(ssh, **kwargs)


# ---------------------------------------------------------------------------
# H1: an untrusted venv is rebuilt at the target version, or the upgrade aborts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUntrustedVenvNeverRunByRoot:
    def test_missing_metadata_rebuilds_at_the_target_version(self) -> None:
        ssh, commands = _recording_ssh({"curl -sf": ("ok", "", 0)}, untrusted_until_rebuild=True)
        result = _upgrade(ssh)
        assert any(re.search(r"venv/bin/pip install .*getdango==1\.0\.15", c) for c in commands)
        assert _old_venv_runs(commands) == []
        assert "rebuilt" in (result.venv_rebuild or "")

    def test_planted_metadata_version_is_not_installed(self) -> None:
        ssh, commands = _recording_ssh(
            {
                "getdango-*.dist-info": ("/v/getdango-99.0.dist-info\n", "", 0),
                "curl -sf": ("ok", "", 0),
            },
            untrusted_until_rebuild=True,
        )
        _upgrade(ssh)
        assert not any("getdango==99.0" in c for c in commands)
        assert _old_venv_runs(commands) == []

    def test_failed_rebuild_aborts_and_never_runs_or_reowns_the_old_venv(self) -> None:
        ssh, commands = _recording_ssh(
            {
                "getdango-*.dist-info": ("/v/getdango-99.0.dist-info\n", "", 0),
                "pip install --no-input getdango==": ("", "no matching distribution", 1),
            },
            untrusted_until_rebuild=True,
        )
        with pytest.raises(CloudError, match="upgrade aborted"):
            _upgrade(ssh)
        assert _old_venv_runs(commands) == [], _old_venv_runs(commands)
        restored = max(
            i for i, c in enumerate(commands) if re.search(r"mv \S+untrusted\S+ /srv/dango/venv", c)
        )
        assert not any("chown -R root:root /srv/dango/venv" in c for c in commands[restored:])
        assert any("systemctl start dango-web" in c for c in commands[restored:])

    def test_rebuilt_venv_with_escaping_links_is_rejected(self) -> None:
        ssh, commands = _recording_ssh(
            {"-print -quit": ("/srv/dango/venv/lib/x.pth -> /srv/dango/project/x\n", "", 0)}
        )
        with pytest.raises(CloudError, match="upgrade aborted"):
            _upgrade(ssh)
        assert _old_venv_runs(commands) == []

    def test_ownership_never_takes_an_untrusted_venv(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh({"-print -quit": ("/srv/dango\n", "", 0)})
        with pytest.raises(ro.VenvRebuildError):
            ro.apply_ownership(ssh, [])
        assert not any("chown -R root:root /srv/dango/venv" in c for c in commands)


@pytest.mark.unit
class TestSymlinkEscapeProbe:
    def _run(self, root: Path) -> str:
        from dango.platform.cloud.runtime_ownership import links_outside_script

        out = subprocess.run(
            ["bash", "-c", links_outside_script(str(root))],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout

    def test_links_inside_or_to_usr_are_fine(self, tmp_path: Path) -> None:
        root = tmp_path / "venv"
        (root / "lib").mkdir(parents=True)
        (root / "lib64").symlink_to(root / "lib")
        (root / "python3").symlink_to("/usr/bin/env")
        assert self._run(root) == ""

    def test_link_out_of_the_venv_is_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "venv"
        root.mkdir()
        outside = tmp_path / "project"
        outside.mkdir()
        (outside / "evil.pth").write_text("import os\n")
        (root / "x.pth").symlink_to(outside / "evil.pth")
        assert "x.pth" in self._run(root)

    def test_probe_and_verify_both_check_links(self) -> None:
        from dango.platform.cloud.runtime_ownership import VENV_TAINT_PROBE, build_verify_script

        assert "readlink -f" in VENV_TAINT_PROBE
        assert "readlink -f" in build_verify_script()


# ---------------------------------------------------------------------------
# H2: with an untrusted venv, nothing reads the version by running it
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVersionCheckWithoutExecution:
    def test_check_versions_reads_metadata_when_untrusted(self) -> None:
        from dango.platform.cloud.upgrade import check_versions

        ssh, commands = _recording_ssh(
            {
                "-print -quit": ("/srv/dango\n", "", 0),
                "getdango-*.dist-info": ("/v/getdango-1.0.13.dist-info\n", "", 0),
            }
        )
        with patch(
            "dango.platform.cloud.server_status.check_latest_pypi_version", return_value="1.0.15"
        ):
            current, _latest = check_versions(ssh)
        assert current == "1.0.13"
        assert [c for c in commands if _VENV_EXEC.search(c)] == []

    def test_cli_upgrade_never_runs_an_untrusted_venv(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from dango.cli.commands.remote import remote

        ssh, commands = _recording_ssh(
            {
                "getdango-*.dist-info": ("/v/getdango-1.0.13.dist-info\n", "", 0),
                "pip install --no-input getdango==": ("", "boom", 1),
            },
            untrusted_until_rebuild=True,
        )
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch(
                "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
                return_value=(MagicMock(provider="digitalocean"), ssh),
            ),
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version",
                return_value="1.0.15",
            ),
        ):
            CliRunner().invoke(remote, ["upgrade", "--yes"], obj={"project_root": tmp_path})
        assert _old_venv_runs(commands) == [], _old_venv_runs(commands)

    def test_step7_version_is_validated(self) -> None:
        ssh, _commands = _recording_ssh(
            {"import dango": ("1.0.15; touch /tmp/x", "", 0), "curl -sf": ("ok", "", 0)}
        )
        result = _upgrade(ssh, latest="1.0.16")
        assert result.new_version is None


# ---------------------------------------------------------------------------
# M1: push never runs an untrusted venv's pip as root
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_push_refuses_requirements_into_an_untrusted_venv(tmp_path: Path) -> None:
    from dango.platform.cloud.deployer import push_deploy
    from tests.unit.test_deployer_push import _make_backup_result, _make_sync_result

    (tmp_path / "requirements.txt").write_text("requests\n")
    ssh, commands = _recording_ssh({"-print -quit": ("/srv/dango\n", "", 0)})
    with (
        patch(
            "dango.platform.cloud.file_sync.sync_project_files", return_value=_make_sync_result()
        ),
        patch("dango.platform.cloud.backup.create_backup", return_value=_make_backup_result()),
    ):
        result = push_deploy(ssh, tmp_path, "10.0.0.1")
    assert not any(re.search(r"/pip install\b", c) for c in commands)
    assert any("dango remote upgrade" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# M2 / L1 / L3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScriptAndRuleHardening:
    def test_backups_deploy_is_guarded_and_chgrp_does_not_follow_links(self) -> None:
        from dango.platform.cloud.runtime_ownership import build_ownership_script

        script = build_ownership_script()
        guard_end = script.index("chown")
        assert "/srv/dango/backups/deploy" in script[:guard_end]
        assert re.search(r"chgrp -h dango ", script)

    @pytest.mark.parametrize(
        "text",
        [
            "-i file:///srv/dango/project/idx\nrequests\n",
            "--extra-index-url ftp://mirror.example.com/simple\n",
            "--find-links file:///tmp/wheels\n",
            "-f file:/tmp/wheels\n",
            "-e \\\n  file:./pkg\n",
            "requests \\\n  -e ./pkg\n",
        ],
    )
    def test_more_refused_requirement_forms(self, text: str) -> None:
        from dango.platform.cloud.runtime_ownership import check_requirements

        assert check_requirements(text)

    def test_continuation_lines_are_still_accepted_when_safe(self) -> None:
        from dango.platform.cloud.runtime_ownership import check_requirements

        assert check_requirements("requests==2.32.3 \\\n    --hash=sha256:abc\n") == []

    def test_verify_covers_units_and_logrotate(self) -> None:
        from dango.platform.cloud.runtime_ownership import build_verify_script

        script = build_verify_script()
        for path in (
            "/etc/systemd/system/dango-web.service",
            "/etc/systemd/system/dango-backup.service",
            "/etc/systemd/system/dango-backup.timer",
            "/etc/logrotate.d/dango",
        ):
            assert path in script, path


_VERIFY_FAILS = {"bad=$(": ("/srv\n", "", 1)}


@pytest.mark.unit
class TestVerificationSeverity:
    def test_managed_setup_fails_on_verification(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_runtime_ownership

        ssh, _ = _recording_ssh({**_VERIFY_FAILS, "-print -quit": ("", "", 0)})
        with pytest.raises(CloudProvisioningError, match="/srv"):
            _setup_runtime_ownership(ssh, SetupResult(), None)

    def test_byos_setup_warns_with_remediation(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, _ = _recording_ssh(
            {
                **_VERIFY_FAILS,
                "-print -quit": ("", "", 0),
                "id -u dango": ("", "", 1),
                "docker --version": ("", "", 1),
                "caddy version": ("", "", 1),
                "test -x /srv/dango/venv/bin/dango": ("", "", 1),
                "cat /": ("", "", 1),
                "ufw status": ("", "", 1),
            }
        )
        result = setup_server(ssh, setup_ufw=True)
        assert any("/srv" in w and "chown root:root" in w for w in result.warnings)

    def test_managed_upgrade_fails_on_verification(self) -> None:
        ssh, commands = _recording_ssh(
            {**_VERIFY_FAILS, "-print -quit": ("", "", 0), "import dango": ("1.0.14", "", 0)}
        )
        with pytest.raises(CloudError, match="/srv"):
            _upgrade(ssh)
        assert any("systemctl start dango-web" in c for c in commands)

    def test_byos_upgrade_warns(self) -> None:
        ssh, _ = _recording_ssh(
            {
                **_VERIFY_FAILS,
                "-print -quit": ("", "", 0),
                "import dango": ("1.0.14", "", 0),
                "curl -sf": ("ok", "", 0),
            }
        )
        result = _upgrade(ssh, byos=True)
        assert any("/srv" in w for w in result.warnings)
