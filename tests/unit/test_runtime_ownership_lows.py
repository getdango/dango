"""tests/unit/test_runtime_ownership_lows.py

Requirement continuations, at-target prompt default, /srv in the trust check, probe sentinel, lock failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_runtime_ownership import _recording_ssh


@pytest.mark.unit
def test_continuation_joined_like_pip_cannot_hide_an_editable_file_url() -> None:
    from dango.platform.cloud.pushed_requirements import check_requirements

    assert check_requirements("-\\\ne fi\\\nle:///srv/dango/project/pkg\n")


@pytest.mark.unit
def test_at_target_rebuild_without_a_tty_does_not_proceed(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from dango.cli.commands.remote import remote

    ssh, _ = _recording_ssh({"getdango-*.dist-info": ("/v/getdango-1.0.15.dist-info\n", "", 0)})
    upgrade = MagicMock()
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(MagicMock(provider="digitalocean"), ssh),
        ),
        patch(
            "dango.platform.cloud.server_status.check_latest_pypi_version", return_value="1.0.15"
        ),
        patch("dango.platform.cloud.runtime_ownership.venv_is_untrusted", return_value=True),
        patch("dango.platform.cloud.upgrade.upgrade_dango", upgrade),
    ):
        result = CliRunner().invoke(remote, ["upgrade"], obj={"project_root": tmp_path})
    assert not upgrade.called
    assert "--yes" in result.output


@pytest.mark.unit
class TestTrustProbe:
    def test_probe_checks_root_and_srv(self) -> None:
        from dango.platform.cloud.runtime_ownership import VENV_TAINT_PROBE

        assert "find / /srv /srv/dango -maxdepth 0" in VENV_TAINT_PROBE

    def test_missing_sentinel_means_untrusted(self) -> None:
        from dango.platform.cloud.runtime_ownership import venv_is_untrusted

        ssh, _ = _recording_ssh({"PROBE_OK": ("", "", 0)})
        assert venv_is_untrusted(ssh)

    def test_nonzero_exit_means_untrusted(self) -> None:
        from dango.platform.cloud.runtime_ownership import PROBE_SENTINEL, venv_is_untrusted

        ssh, _ = _recording_ssh({"PROBE_OK": (PROBE_SENTINEL + "\n", "", 2)})
        assert venv_is_untrusted(ssh)

    def test_sentinel_alone_means_trusted(self) -> None:
        from dango.platform.cloud.runtime_ownership import PROBE_SENTINEL, venv_is_untrusted

        ssh, _ = _recording_ssh({"PROBE_OK": (PROBE_SENTINEL + "\n", "", 0)})
        assert not venv_is_untrusted(ssh)

    def test_probe_script_executes_and_ends_with_the_sentinel(self, tmp_path: Path) -> None:
        from dango.platform.cloud.runtime_ownership import PROBE_SENTINEL, VENV_TAINT_PROBE

        srv = tmp_path / "srvd"
        (srv / "venv").mkdir(parents=True)
        run = subprocess.run(
            ["bash", "-c", VENV_TAINT_PROBE.replace("/srv/dango", str(srv))],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "syntax error" not in run.stderr
        lines = run.stdout.strip().splitlines()
        # Not root-owned locally: findings are printed (or the probe fails), never only the sentinel.
        assert lines != [PROBE_SENTINEL]


@pytest.mark.unit
class TestRebuildGuards:
    def test_failed_root_lock_stops_the_rebuild_before_moving_anything(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, commands = _recording_ssh(
            {"install -d -o root -g root -m 0755 /srv/dango": ("", "denied", 1)},
            untrusted_until_rebuild=True,
        )
        with pytest.raises(ro.VenvRebuildError, match="/srv/dango"):
            ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")
        assert not any(c.startswith("mv /srv/dango/venv") for c in commands)

    def test_lock_step_strips_default_acls_when_setfacl_exists(self) -> None:
        from dango.platform.cloud.runtime_ownership import ownership_steps

        srv = next(s for s in ownership_steps() if s.name == "srv_dango")
        assert "setfacl -k /srv/dango" in srv.script

    def test_post_rebuild_failure_names_a_default_acl(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, _ = _recording_ssh(
            {
                "-print -quit": ("/srv/dango/venv/bin\nDANGO_PROBE_OK\n", "", 0),
                "getfacl": ("default:user::rwx\ndefault:group:dango:rwx\n", "", 0),
            }
        )
        with pytest.raises(ro.VenvRebuildError, match=r"setfacl -b /srv/dango"):
            ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")

    def test_byos_untrusted_srv_is_refused_with_remediation(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        ssh, _ = _recording_ssh({"-print -quit": ("/srv\nDANGO_PROBE_OK\n", "", 0)})
        with pytest.raises(ro.VenvRebuildError, match=r"chown root:root /srv"):
            ro.rebuild_untrusted_venv(ssh, [], version="1.0.15")
