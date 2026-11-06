"""tests/unit/test_backup_notices_cli.py

What the CLI says about off-site backups: the on-demand backup, backup enable and remote status (AF-10 follow-up, G3F4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.platform.cloud.ssh import CommandResult
from tests.factories.backup_cli import (
    CTX,
    DOWNLOAD_HINT,
    ENV_WITH_KEYS,
    LOADER,
    SSH,
    invoke,
    make_cfg,
    make_loader,
    make_ssh,
    plain,
)

# ---------------------------------------------------------------------------
# remote backup (on demand): truthful off-site line
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOnDemandBackupOffsite:
    def test_off_site_yes(self, tmp_path: Path) -> None:
        out = "Backup complete in 3s\nOff-site: yes (Spaces my-bucket)\n"
        result = invoke(remote, ["backup"], tmp_path, make_cfg(), make_ssh(run_stdout=out))
        assert result.exit_code == 0, result.output
        text = plain(result.output)
        assert "Off-site: yes (Spaces my-bucket)" in text
        assert DOWNLOAD_HINT not in text

    def test_off_site_no_prints_the_download_command(self, tmp_path: Path) -> None:
        out = "Backup complete in 3s\nOff-site: NO: backups exist only on this server and are lost with it\n"
        result = invoke(
            remote,
            ["backup"],
            tmp_path,
            make_cfg(spaces=False, provider="byos"),
            make_ssh(run_stdout=out),
        )
        assert result.exit_code == 0, result.output
        text = plain(result.output)
        assert "Off-site: NO" in text
        assert "lost with it" in text
        assert DOWNLOAD_HINT in text

    def test_a_server_that_does_not_report_is_not_called_off_site(self, tmp_path: Path) -> None:
        result = invoke(
            remote, ["backup"], tmp_path, make_cfg(), make_ssh(run_stdout="Backup complete in 3s\n")
        )
        text = plain(result.output)
        assert "Off-site: yes" not in text
        assert "Off-site: unknown" in text


# ---------------------------------------------------------------------------
# remote backup enable
# ---------------------------------------------------------------------------


def _env_writes(ssh: Any) -> list[str]:
    return [
        str(c.args[1]) for c in ssh.write_remote_file.call_args_list if "SPACES_" in str(c.args[1])
    ]


@pytest.mark.unit
class TestBackupEnable:
    def test_the_destination_is_written_to_the_server_env(self, tmp_path: Path) -> None:
        ssh = make_ssh(env_text="FOO=1\n" + ENV_WITH_KEYS)
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        assert result.exit_code == 0, result.output
        written = _env_writes(ssh)
        assert len(written) == 1
        assert written[0].startswith("FOO=1\n")
        assert "SPACES_BUCKET=my-bucket\n" in written[0]
        assert "SPACES_REGION=sgp1\n" in written[0]
        assert "Off-site: yes (Spaces my-bucket)" in plain(result.output)

    def test_an_env_without_a_final_newline_is_not_glued(self, tmp_path: Path) -> None:
        ssh = make_ssh(env_text="SPACES_ACCESS_KEY=ak\nSPACES_SECRET_KEY=sk")
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        assert result.exit_code == 0, result.output
        assert "SPACES_SECRET_KEY=sk\nSPACES_BUCKET=my-bucket\n" in _env_writes(ssh)[0]

    def test_an_existing_destination_is_not_rewritten(self, tmp_path: Path) -> None:
        env = ENV_WITH_KEYS + "SPACES_BUCKET=deployed-bucket\nSPACES_REGION=nyc3\n"
        ssh = make_ssh(env_text=env)
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        assert result.exit_code == 0, result.output
        assert _env_writes(ssh) == []
        assert "Off-site: yes (Spaces deployed-bucket)" in plain(result.output)

    def test_missing_keys_still_fail(self, tmp_path: Path) -> None:
        result = invoke(
            remote, ["backup", "enable"], tmp_path, make_cfg(), make_ssh(env_text="FOO=1\n")
        )
        assert result.exit_code == 1
        assert "SPACES_ACCESS_KEY" in result.output

    def test_an_invalid_server_bucket_is_refused_before_the_timer_is_enabled(
        self, tmp_path: Path
    ) -> None:
        ssh = make_ssh(env_text=ENV_WITH_KEYS + "SPACES_BUCKET=bad bucket;id\nSPACES_REGION=sgp1\n")
        result = invoke(remote, ["backup", "enable"], tmp_path, make_cfg(), ssh)
        assert result.exit_code == 1
        assert "SPACES_BUCKET" in result.output
        assert not any("enable --now" in str(c) for c in ssh.exec_command.call_args_list)

    def test_byos_enables_a_local_only_timer_and_says_so(self, tmp_path: Path) -> None:
        ssh = make_ssh(env_text="FOO=1\n")
        result = invoke(
            remote, ["backup", "enable"], tmp_path, make_cfg(spaces=False, provider="byos"), ssh
        )
        assert result.exit_code == 0, result.output
        text = plain(result.output)
        assert "Off-site: NO" in text
        assert "lost with it" in text
        assert DOWNLOAD_HINT in text
        assert any("enable --now" in str(c) for c in ssh.exec_command.call_args_list)


# ---------------------------------------------------------------------------
# remote status
# ---------------------------------------------------------------------------


def _status(tmp_path: Path, ssh: Any, cfg: Any | None = None) -> str:
    from dango.platform.cloud.server_status import ServerStatus

    with (
        patch(CTX, return_value=tmp_path),
        patch(LOADER, return_value=make_loader(cfg or make_cfg())),
        patch(SSH, return_value=ssh),
        patch(
            "dango.platform.cloud.server_status.collect_server_status",
            return_value=ServerStatus(dango_version="1.0.14"),
        ),
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=None),
    ):
        result = CliRunner().invoke(
            remote, ["status"], obj={"project_root": tmp_path}, catch_exceptions=False
        )
    assert result.exit_code == 0, result.output
    return plain(result.output)


@pytest.mark.unit
class TestRemoteStatusOffsite:
    def test_off_site_yes(self, tmp_path: Path) -> None:
        env = ENV_WITH_KEYS + "SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"
        text = _status(tmp_path, make_ssh(env_text=env))
        assert "Off-site: yes (Spaces my-bucket)" in text

    def test_off_site_no_when_the_server_has_no_destination(self, tmp_path: Path) -> None:
        text = _status(
            tmp_path, make_ssh(env_text="FOO=1\n"), make_cfg(spaces=False, provider="byos")
        )
        assert "Off-site: NO" in text
        assert DOWNLOAD_HINT in text

    def test_a_destination_without_keys_is_not_off_site(self, tmp_path: Path) -> None:
        text = _status(tmp_path, make_ssh(env_text="SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"))
        assert "Off-site: yes" not in text
        assert "Off-site: NO" in text

    def test_the_timer_state_is_shown(self, tmp_path: Path) -> None:
        env = ENV_WITH_KEYS + "SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"
        assert "not enabled" in _status(tmp_path, make_ssh(env_text=env, timer="inactive")).lower()

    def test_an_unreadable_server_env_is_unknown_not_yes(self, tmp_path: Path) -> None:
        ssh = MagicMock()
        ssh.connect.return_value = ssh
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="boom", exit_code=1)
        text = _status(tmp_path, ssh)
        assert "Off-site: yes" not in text
        assert "Off-site: unknown" in text
