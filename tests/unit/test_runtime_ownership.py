"""tests/unit/test_runtime_ownership.py

Cloud servers: /srv/dango and the venv are root-owned; root installs only pushed requirements.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud.ssh import CommandResult

_PROJECT = "/srv/dango/project"


def _recording_ssh(
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[MagicMock, list[str]]:
    """Return an SSH mock plus the list every executed command is appended to."""
    results = exec_results or {}
    commands: list[str] = []

    def _exec(command: str, *args: object, **kwargs: object) -> CommandResult:
        commands.append(command)
        for substr, (stdout, stderr, code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    ssh.write_remote_file = MagicMock()
    return ssh, commands


def _written(ssh: MagicMock) -> dict[str, str]:
    return {c.args[0]: c.args[1] for c in ssh.write_remote_file.call_args_list}


# ---------------------------------------------------------------------------
# Ownership script (composable steps) and verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOwnershipScript:
    def test_script_makes_runtime_root_owned(self) -> None:
        from dango.platform.cloud.runtime_ownership import build_ownership_script

        script = build_ownership_script()
        assert script.startswith("set -e")
        assert re.search(r"chown root:root /srv/dango(\s|;|&|$)", script)
        assert re.search(r"chown -R root:root /srv/dango/venv\b", script)
        assert "chmod -R go-w /srv/dango/venv" in script
        assert "install -d -o dango -g dango -m 0700 /srv/dango/secrets" in script
        assert re.search(r"chgrp dango /srv/dango/backups /srv/dango/backups/deploy", script)
        assert "chmod 0750 /srv/dango/backups /srv/dango/backups/deploy" in script
        assert re.search(r"find /srv/dango/backups -type f -exec chmod 0600", script)
        assert re.search(r"chown -R dango:dango /srv/dango/project\b", script)
        assert not re.search(r"chown\s+(-R\s+)?dango:dango\s+/srv/dango(\s|$)", script)

    def test_script_refuses_symlinked_runtime_paths_before_changing_anything(self) -> None:
        from dango.platform.cloud.runtime_ownership import build_ownership_script

        script = build_ownership_script()
        guard = script.index("-L")
        assert guard < script.index("chown")
        for path in ("/srv/dango/venv", "/srv/dango/secrets", "/srv/dango/backups", _PROJECT):
            assert path in script[: script.index("chown")]

    def test_steps_are_composable(self) -> None:
        from dango.platform.cloud.runtime_ownership import (
            OwnershipStep,
            build_ownership_script,
            ownership_steps,
        )

        names = [s.name for s in ownership_steps()]
        assert names[0] == "symlink_guard"
        assert {"srv_dango", "venv", "secrets", "backups", "project"} <= set(names)
        extra = OwnershipStep("runner_users", "echo extra-step")
        script = build_ownership_script(extra_steps=[extra])
        assert script.index("echo extra-step") > script.index("chown -R dango:dango")
        assert build_ownership_script() == build_ownership_script()

    def test_verify_script_checks_the_whole_root_execution_chain(self) -> None:
        from dango.platform.cloud.runtime_ownership import build_verify_script

        script = build_verify_script()
        for path in ("/ ", "/srv ", "/srv/dango ", "/srv/dango/venv"):
            assert path in script, path
        assert "-perm /022" in script
        assert "! -user root" in script


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

_FRESH_SERVER = {
    "id -u dango": ("", "no such user", 1),
    "docker --version": ("", "", 1),
    "caddy version": ("", "", 1),
    "test -x /srv/dango/venv/bin/dango": ("", "", 1),
    "cat /": ("", "No such file", 1),
}


@pytest.mark.unit
class TestSetupOwnership:
    def test_setup_never_hands_srv_dango_to_dango(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        setup_server(ssh)
        pattern = r"chown\s+(-R\s+)?dango:dango\s+/srv/dango(\s|$|/venv|/backups)"
        assert [c for c in commands if re.search(pattern, c)] == []

    def test_setup_makes_runtime_root_owned_after_venv_install(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        result = setup_server(ssh)
        venv_created = next(
            i for i, c in enumerate(commands) if "python3 -m venv /srv/dango/venv" in c
        )
        later = " ; ".join(commands[venv_created + 1 :])
        assert re.search(r"chown -R root:root /srv/dango/venv\b", later), later
        assert re.search(r"chown root:root /srv/dango(\s|;|&|$)", later), later
        assert "runtime_ownership" in result.steps_completed

    def test_setup_keeps_project_and_secrets_dango_owned(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        setup_server(ssh)
        joined = " ; ".join(commands)
        assert re.search(r"chown -R dango:dango /srv/dango/project\b", joined)
        assert "install -d -o dango -g dango -m 0700 /srv/dango/secrets" in joined

    def test_setup_reports_a_failed_verification_as_a_warning(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, _commands = _recording_ssh({**_FRESH_SERVER, "-perm /022": ("/srv\n", "", 1)})
        result = setup_server(ssh)
        assert any("/srv" in w for w in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# Upgrade: ownership, backup unit, automatic venv rebuild
# ---------------------------------------------------------------------------


def _run_upgrade(
    installed: str,
    latest: str,
    *,
    backup_unit_present: bool = False,
    venv_untrusted: bool = False,
    extra: dict[str, tuple[str, str, int]] | None = None,
    local_project_root: Path | None = None,
) -> tuple[MagicMock, list[str], Any]:
    from dango.platform.cloud.upgrade import upgrade_dango

    results = {
        **(extra or {}),
        "import dango": (installed, "", 0),
        "test -f /etc/systemd/system/dango-backup.service": (
            "",
            "",
            0 if backup_unit_present else 1,
        ),
        "-print -quit": ("/srv/dango/venv/bin/x\n" if venv_untrusted else "", "", 0),
        "curl -sf": ("ok", "", 0),
    }
    ssh, commands = _recording_ssh(results)
    backup_result = MagicMock(archive_path="/srv/dango/backups/deploy/backup-x.tar.gz")
    with (
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=latest),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup_result),
    ):
        if local_project_root is None:
            result = upgrade_dango(ssh)
        else:
            result = upgrade_dango(ssh, local_project_root=local_project_root)
    return ssh, commands, result


@pytest.mark.unit
class TestUpgradeOwnership:
    def test_upgrade_makes_runtime_root_owned(self) -> None:
        _ssh, commands, result = _run_upgrade("1.0.14", "1.0.15")
        joined = " ; ".join(commands)
        assert re.search(r"chown -R root:root /srv/dango/venv\b", joined), joined
        assert re.search(r"chown root:root /srv/dango(\s|;|&|$)", joined), joined
        assert result.runtime_ownership

    def test_upgrade_rewrites_existing_backup_unit(self) -> None:
        from dango.platform.cloud._server_templates import SYSTEMD_BACKUP_SERVICE

        ssh, commands, _result = _run_upgrade("1.0.14", "1.0.15", backup_unit_present=True)
        assert _written(ssh).get("/etc/systemd/system/dango-backup.service") == (
            SYSTEMD_BACKUP_SERVICE
        )
        assert any("systemctl daemon-reload" in c for c in commands)

    def test_upgrade_leaves_absent_backup_unit_absent(self) -> None:
        ssh, _commands, _result = _run_upgrade("1.0.14", "1.0.15", backup_unit_present=False)
        assert "/etc/systemd/system/dango-backup.service" not in _written(ssh)

    def test_ownership_failure_is_a_warning_and_services_restart(self) -> None:
        _ssh, commands, result = _run_upgrade(
            "1.0.14", "1.0.15", extra={"set -e": ("", "chown: boom", 1)}
        )
        assert any("ownership" in w.lower() for w in result.warnings)
        assert any("systemctl start dango-web" in c for c in commands)


@pytest.mark.unit
class TestUpgradeVenvRebuild:
    def test_untrusted_venv_is_rebuilt_in_place_and_verified(self) -> None:
        _ssh, commands, result = _run_upgrade("1.0.14", "1.0.15", venv_untrusted=True)
        joined = "\n".join(commands)
        assert re.search(r"mv /srv/dango/venv /srv/dango/venv\.untrusted-\S+", joined), joined
        assert "python3 -m venv /srv/dango/venv" in joined
        assert re.search(r"/srv/dango/venv/bin/pip install .*getdango==1\.0\.15", joined)
        assert '/srv/dango/venv/bin/python -I -c "import dango' in joined
        assert re.search(r"rm -rf /srv/dango/venv\.untrusted-\S+", joined)
        assert "rebuilt" in (result.venv_rebuild or "")

    def test_failed_rebuild_keeps_the_old_venv_and_warns(self) -> None:
        _ssh, commands, result = _run_upgrade(
            "1.0.14",
            "1.0.15",
            venv_untrusted=True,
            extra={"python3 -m venv /srv/dango/venv": ("", "no space", 1)},
        )
        joined = "\n".join(commands)
        assert re.search(r"mv /srv/dango/venv\.untrusted-\S+ /srv/dango/venv", joined), joined
        assert "kept" in (result.venv_rebuild or "")
        assert any("venv" in w for w in result.warnings)
        assert any("systemctl start dango-web" in c for c in commands)

    def test_trusted_venv_is_not_rebuilt(self) -> None:
        _ssh, commands, result = _run_upgrade("1.0.14", "1.0.15", venv_untrusted=False)
        assert not any("venv.untrusted" in c for c in commands)
        assert result.venv_rebuild is None

    def test_rebuild_reinstalls_pushed_requirements(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        ssh, commands, _result = _run_upgrade(
            "1.0.14", "1.0.15", venv_untrusted=True, local_project_root=tmp_path
        )
        uploads = [p for p in _written(ssh) if p.endswith("requirements.txt")]
        assert uploads and all(p.startswith("/tmp/dango-req-") for p in uploads), uploads
        assert any(
            re.search(r"/srv/dango/venv/bin/pip install .*-r /tmp/dango-req-", c) for c in commands
        )


# ---------------------------------------------------------------------------
# Requirements installed by root come only from the pushed local files (D2 rules)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequirementsRules:
    @pytest.mark.parametrize(
        "line",
        [
            "-e .",
            "--editable ./pkg",
            "-r other.txt",
            "--requirement=base.txt",
            "-c constraints.txt",
            "./local_pkg",
            "../sibling",
            "/opt/wheels/x-1.0-py3-none-any.whl",
            "x-1.0-py3-none-any.whl",
            "pkg @ file:///srv/dango/project/pkg",
            "file:pkg",
            "git+https://user:token@github.com/org/repo.git",
            "pkg @ https://token@example.com/pkg.tar.gz",
            "--index-url https://u:p@pypi.example.com/simple",
            "--find-links ./wheels",
        ],
    )
    def test_refused_lines(self, line: str) -> None:
        from dango.platform.cloud.runtime_ownership import check_requirements

        assert check_requirements(f"requests==2.32.3\n{line}\n")

    @pytest.mark.parametrize(
        "text",
        [
            "requests==2.32.3\n# comment\n\npandas>=2 ; python_version >= '3.10'\n",
            "stripe[async]~=10.0  # payments\n",
            "pkg @ https://example.com/pkg-1.0.tar.gz\n",
            "git+https://github.com/org/repo.git@v1#egg=repo\n",
            "--index-url https://pypi.example.com/simple\nrequests\n",
            "requests==2.32.3 --hash=sha256:abc\n",
        ],
    )
    def test_accepted_files(self, text: str) -> None:
        from dango.platform.cloud.runtime_ownership import check_requirements

        assert check_requirements(text) == []

    def test_refusal_message_does_not_echo_credentials(self) -> None:
        from dango.platform.cloud.runtime_ownership import check_requirements

        refused = check_requirements("git+https://user:s3cret@github.com/org/repo.git\n")
        assert refused and all("s3cret" not in r for r in refused)


def _push(tmp_path: Path, files: dict[str, str]) -> tuple[MagicMock, list[str], Any]:
    from dango.platform.cloud.deployer import push_deploy
    from tests.unit.test_deployer_push import _make_backup_result, _make_sync_result

    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    ssh, commands = _recording_ssh({"requirements.txt && echo exists": ("exists", "", 0)})
    with (
        patch(
            "dango.platform.cloud.file_sync.sync_project_files", return_value=_make_sync_result()
        ),
        patch("dango.platform.cloud.backup.create_backup", return_value=_make_backup_result()),
    ):
        result = push_deploy(ssh, tmp_path, "10.0.0.1")
    return ssh, commands, result


@pytest.mark.unit
class TestPushRequirements:
    def test_requirements_installed_by_root_from_the_pushed_files(self, tmp_path: Path) -> None:
        ssh, commands, _result = _push(
            tmp_path,
            {"requirements.txt": "requests==2.32.3\n", "scripts/requirements.txt": "rich\n"},
        )
        pip_runs = [c for c in commands if re.search(r"/pip\s+install\b.*-r\b", c)]
        assert len(pip_runs) == 2, commands
        for command in pip_runs:
            assert not re.search(r"sudo\s+-u\s+dango", command), command
            assert _PROJECT not in command, command
            assert re.search(r"-r /tmp/dango-req-[0-9a-f]{32}/", command), command
        uploaded = _written(ssh)
        assert "requests==2.32.3\n" in uploaded.values()
        assert "rich\n" in uploaded.values()

    def test_refused_requirements_are_not_installed(self, tmp_path: Path) -> None:
        _ssh, commands, result = _push(tmp_path, {"requirements.txt": "-e ./mypkg\n"})
        assert not any(re.search(r"/pip\s+install\b", c) for c in commands), commands
        assert any("requirements.txt" in w for w in result.warnings)

    def test_no_local_requirements_means_no_install(self, tmp_path: Path) -> None:
        _ssh, commands, _result = _push(tmp_path, {})
        assert not any(re.search(r"/pip\s+install\b", c) for c in commands), commands


# ---------------------------------------------------------------------------
# Already at the target version: idempotent post-install steps still run
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAtTarget:
    def test_upgrade_at_target_still_applies_ownership_without_pip(self) -> None:
        _ssh, commands, result = _run_upgrade("1.0.15", "1.0.15")
        joined = " ; ".join(commands)
        assert re.search(r"chown -R root:root /srv/dango/venv\b", joined), joined
        assert not any(re.search(r"/pip install\b.*getdango", c) for c in commands)
        assert result.runtime_ownership

    def test_upgrade_at_target_rebuilds_an_untrusted_venv(self) -> None:
        _ssh, commands, result = _run_upgrade("1.0.15", "1.0.15", venv_untrusted=True)
        joined = "\n".join(commands)
        assert re.search(r"/srv/dango/venv/bin/pip install .*getdango==1\.0\.15", joined)
        assert "rebuilt" in (result.venv_rebuild or "")
        assert any("systemctl start dango-web" in c for c in commands)


def _cli_upgrade(
    tmp_path: Path, current: str, latest: str, args: list[str]
) -> tuple[MagicMock, list[str], str, MagicMock]:
    from click.testing import CliRunner

    from dango.cli.commands.remote import remote

    ssh, commands = _recording_ssh(
        {
            "import dango": (current, "", 0),
            "cat /etc/caddy/Caddyfile": (":80 {\n}\n", "", 0),
            "curl -sf": ("ok", "", 0),
        }
    )
    spy = MagicMock()
    import dango.platform.cloud.upgrade as upgrade_module

    real = upgrade_module.upgrade_dango

    def _spy(*a: Any, **k: Any) -> Any:
        spy(*a, **k)
        return real(*a, **k)

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(MagicMock(), ssh),
        ),
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=latest),
        patch(
            "dango.platform.cloud.backup.create_backup", return_value=MagicMock(archive_path="a")
        ),
        patch("dango.platform.cloud.upgrade.upgrade_dango", side_effect=_spy),
    ):
        result = CliRunner().invoke(
            remote, args, obj={"project_root": tmp_path}, catch_exceptions=False
        )
    return ssh, commands, result.output, spy


@pytest.mark.unit
class TestRemoteUpgradeCli:
    def test_at_target_runs_post_install_steps_without_pip(self, tmp_path: Path) -> None:
        _ssh, commands, output, _spy = _cli_upgrade(tmp_path, "1.0.15", "1.0.15", ["upgrade"])
        assert "no upgrade needed" in output.lower()
        assert any("cat /etc/caddy/Caddyfile" in c for c in commands), commands
        assert any(re.search(r"chown root:root /srv/dango(\s|;|&|$)", c) for c in commands)
        assert not any(re.search(r"/pip install\b.*getdango", c) for c in commands)

    def test_project_root_is_passed_for_requirements(self, tmp_path: Path) -> None:
        _ssh, _commands, _output, spy = _cli_upgrade(
            tmp_path, "1.0.14", "1.0.15", ["upgrade", "--yes"]
        )
        assert spy.call_args.kwargs.get("local_project_root") == tmp_path
