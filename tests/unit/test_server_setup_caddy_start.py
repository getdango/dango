"""tests/unit/test_server_setup_caddy_start.py

Caddy is stopped right after its package install and started only once the Caddyfile is in place,
so the package's default welcome page is never served (server_setup.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.factories.cloud_factories import host_python_server, make_ssh_mock_configurable


@pytest.fixture(autouse=True)
def _trusted_server_venv() -> Any:
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        host_python_server(),
    ):
        yield


def _commands(ssh: Any) -> list[str]:
    return [c.args[0] for c in ssh.exec_command.call_args_list]


@pytest.mark.unit
class TestCaddyNotServedBeforeCaddyfile:
    def test_fresh_install_stops_caddy_after_apt_install(self):
        from dango.platform.cloud.server_setup import SetupResult, _setup_caddy

        ssh = make_ssh_mock_configurable(exec_results={"caddy version": ("", "", 1)})
        _setup_caddy(ssh, SetupResult(), None)

        install = [c for c in _commands(ssh) if "apt-get install -y -qq caddy" in c]
        assert len(install) == 1
        assert install[0].rindex("systemctl stop caddy") > install[0].index(
            "apt-get install -y -qq caddy"
        )

    def test_already_installed_caddy_is_left_running(self):
        from dango.platform.cloud.server_setup import SetupResult, _setup_caddy

        ssh = make_ssh_mock_configurable(exec_results={"caddy version": ("v2.7", "", 0)})
        _setup_caddy(ssh, SetupResult(), None)

        assert not [c for c in _commands(ssh) if "systemctl stop caddy" in c]

    def test_caddyfile_write_precedes_caddy_start_on_fresh_server(self):
        from dango.platform.cloud.server_setup import setup_server

        ssh = make_ssh_mock_configurable(
            exec_results={
                "id -u dango": ("", "no such user", 1),
                "docker --version": ("", "", 1),
                "caddy version": ("", "", 1),
                "test -x /srv/dango/venv/bin/dango": ("", "", 1),
                "cat /etc/caddy/Caddyfile": ("", "", 1),
            }
        )
        setup_server(ssh)

        events: list[str] = []
        for call in ssh.mock_calls:
            if call[0] == "exec_command" and "systemctl stop caddy" in call[1][0]:
                events.append("stop")
            elif call[0] == "write_remote_file" and call[1][0] == "/etc/caddy/Caddyfile":
                events.append("write")
            elif call[0] == "exec_command" and (
                "systemctl reload-or-restart caddy" in call[1][0]
                or "systemctl start caddy" in call[1][0]
            ):
                events.append("start")
        assert events == ["stop", "write", "start"]

    def test_unchanged_caddyfile_still_ensures_caddy_is_running(self):
        """A re-run after a failure between Caddyfile write and start must not leave Caddy stopped."""
        from dango.platform.cloud._server_templates import build_caddyfile
        from dango.platform.cloud.server_setup import SetupResult, _setup_caddyfile

        ssh = make_ssh_mock_configurable(
            exec_results={"cat /etc/caddy/Caddyfile": (build_caddyfile(None), "", 0)}
        )
        _setup_caddyfile(ssh, SetupResult(), None, domain=None)

        ssh.write_remote_file.assert_not_called()
        assert [c for c in _commands(ssh) if "systemctl is-active --quiet caddy" in c]
