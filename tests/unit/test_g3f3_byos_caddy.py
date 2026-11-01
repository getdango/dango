"""tests/unit/test_g3f3_byos_caddy.py

BYOS refuses a dango-owned /srv/dango before any root step; Caddy answers a controlled 503 while starting.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult

_ME = getpass.getuser()


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.unit
class TestPreflightScript:
    """The shell check, run for real on temp paths (the owner stands in for root)."""

    @staticmethod
    def _script(*paths: Path, owner: str = _ME) -> str:
        from dango.platform.cloud.srv_preflight import preflight_script

        return preflight_script(tuple(str(p) for p in paths), owner=owner)

    def test_absent_paths_pass(self, tmp_path: Path) -> None:
        assert _run(self._script(tmp_path / "srv", tmp_path / "srv" / "dango")).returncode == 0

    def test_owned_closed_directories_pass(self, tmp_path: Path) -> None:
        srv = tmp_path / "srv"
        (srv / "dango").mkdir(parents=True)
        for path in (srv, srv / "dango"):
            path.chmod(0o755)
        assert _run(self._script(srv, srv / "dango")).returncode == 0

    def test_directory_of_another_owner_is_refused_and_named(self, tmp_path: Path) -> None:
        (tmp_path / "dango").mkdir(mode=0o755)
        result = _run(self._script(tmp_path / "dango", owner=str(os.getuid() + 1)))
        assert result.returncode == 3
        assert str(tmp_path / "dango") in result.stdout

    @pytest.mark.parametrize("mode", [0o775, 0o757, 0o777])
    def test_group_or_other_writable_is_refused(self, tmp_path: Path, mode: int) -> None:
        target = tmp_path / "dango"
        target.mkdir()
        target.chmod(mode)
        assert _run(self._script(target)).returncode == 3

    def test_symlink_is_refused_even_to_a_closed_directory(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o755)
        link = tmp_path / "dango"
        link.symlink_to(real)
        assert _run(self._script(link)).returncode == 3

    def test_regular_file_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "dango"
        target.write_text("x")
        assert _run(self._script(target)).returncode == 3

    def test_the_parent_is_checked_too(self, tmp_path: Path) -> None:
        srv = tmp_path / "srv"
        (srv / "dango").mkdir(parents=True)
        (srv / "dango").chmod(0o755)
        srv.chmod(0o777)
        assert _run(self._script(srv, srv / "dango")).returncode == 3

    def test_default_paths_are_srv_and_srv_dango(self) -> None:
        from dango.platform.cloud.srv_preflight import preflight_script

        script = preflight_script()
        assert "/srv " in script and "/srv/dango" in script
        assert "-user root" in script


def _is_check(command: str) -> bool:
    from dango.platform.cloud.srv_preflight import preflight_script

    return shlex.quote(preflight_script()) in command


def _byos_ssh(unsafe: bool) -> tuple[MagicMock, list[str]]:
    commands: list[str] = []

    def _exec(command: str, **kwargs: object) -> CommandResult:
        commands.append(command)
        if unsafe and _is_check(command):
            return CommandResult(
                stdout="drwxr-xr-x 2 dango dango 4096 Oct 31 /srv/dango\n", stderr="", exit_code=3
            )
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    return ssh, commands


@pytest.mark.unit
class TestByosSetupRefuses:
    def test_unsafe_srv_dango_is_refused_before_any_other_step(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _byos_ssh(unsafe=True)
        with pytest.raises(CloudProvisioningError, match=r"/srv/dango") as excinfo:
            setup_server(ssh, setup_ufw=True)
        assert "dango dango" in str(excinfo.value)
        assert "chown root:root" in str(excinfo.value)
        assert len(commands) == 1, commands
        ssh.write_remote_file.assert_not_called()

    def test_safe_host_continues_into_setup(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _byos_ssh(unsafe=False)
        with contextlib.suppress(Exception):  # a loose mock may stop a later step
            setup_server(ssh, setup_ufw=True)
        assert _is_check(commands[0])
        assert len(commands) > 1

    def test_a_check_that_cannot_run_refuses_too(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="boom", exit_code=2)
        with pytest.raises(CloudProvisioningError, match=r"could not be checked"):
            setup_server(ssh, setup_ufw=True)
        assert ssh.exec_command.call_count == 1

    def test_digitalocean_setup_does_not_run_the_check(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _byos_ssh(unsafe=True)
        with contextlib.suppress(Exception):  # a loose mock may stop a later step
            setup_server(ssh)
        assert not any(_is_check(c) for c in commands)


# ---------------------------------------------------------------------------
# AF-19: a controlled 503 while dango-web is not up yet
# ---------------------------------------------------------------------------

_ERRORS = (
    "\thandle_errors {\n"
    "\t\t@starting expression `{http.error.status_code} == 502"
    " || {http.error.status_code} == 503 || {http.error.status_code} == 504`\n"
    "\t\thandle @starting {\n"
    '\t\t\theader Retry-After "30"\n'
    '\t\t\theader Cache-Control "no-store"\n'
    '\t\t\trespond "Dango is starting. Please try again in a minute." 503\n'
    "\t\t}\n"
    "\t}\n"
)

#: ``build_caddyfile`` output at 0a3574c9, before the error handler existed.
_OLD_HTTP = (
    ":80 {\n"
    "\thandle /notebooks/marimo/ws {\n"
    "\t\tforward_auth localhost:8800 {\n"
    "\t\t\turi /api/notebooks/ws-auth\n"
    "\t\t\theader_up -Connection\n"
    "\t\t\theader_up -Upgrade\n"
    "\t\t\theader_up -Sec-WebSocket-Key\n"
    "\t\t\theader_up -Sec-WebSocket-Version\n"
    "\t\t\theader_up -Sec-WebSocket-Extensions\n"
    "\t\t}\n"
    "\t\treverse_proxy localhost:7805\n"
    "\t}\n"
    "\thandle {\n"
    "\t\treverse_proxy localhost:8800\n"
    "\t}\n"
    "\theader {\n"
    "\t\tX-Content-Type-Options nosniff\n"
    "\t\tX-Frame-Options SAMEORIGIN\n"
    "\t\tReferrer-Policy strict-origin-when-cross-origin\n"
    '\t\tPermissions-Policy "interest-cohort=()"\n'
    "\t}\n"
    "}\n"
)
_OLD_HTTPS = _OLD_HTTP.replace(":80 {", "app.example.com {", 1).replace(
    '"interest-cohort=()"\n\t}\n}\n',
    '"interest-cohort=()"\n'
    '\t\tStrict-Transport-Security "max-age=63072000; includeSubDomains"\n'
    "\t}\n}\n"
    '\n:80 {\n\trespond "Use the configured domain to access this server." 403\n}\n',
)


@pytest.mark.unit
class TestStartingPage:
    def test_http_only_caddyfile_is_the_old_one_plus_the_error_handler(self) -> None:
        from dango.platform.cloud._server_templates import build_caddyfile

        new = build_caddyfile()
        assert new.replace(_ERRORS, "") == _OLD_HTTP
        assert _ERRORS in new

    def test_domain_caddyfile_is_the_old_one_plus_the_error_handler(self) -> None:
        from dango.platform.cloud._server_templates import build_caddyfile

        new = build_caddyfile("app.example.com")
        assert new.replace(_ERRORS, "") == _OLD_HTTPS
        assert new.count("handle_errors") == 1  # the bare :80 refusal block is unchanged

    @pytest.mark.parametrize("domain", [None, "app.example.com"])
    def test_the_page_leaks_nothing(self, domain: str | None) -> None:
        from dango.platform.cloud._server_templates import build_caddyfile

        block = re.search(r"\thandle_errors \{.*?\n\t\}\n", build_caddyfile(domain), re.S)
        assert block is not None
        text = block.group(0)
        assert "Retry-After" in text and " 503" in text
        assert "{http.error.message}" not in text and "{err." not in text
        assert "version" not in text.lower()
        assert "reverse_proxy" not in text
