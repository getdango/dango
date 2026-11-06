"""tests/unit/test_g3f6_messages.py

Three message fixes from the Gate 3 rerun: the BYOS backup notice, the missing-helper recovery
text, and the loopback line of an unsupported isolation status.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud.isolation_status import IsolationStatus
from tests.factories.remote_isolation_fakes import IsoSSH, invoke

pytestmark = pytest.mark.unit


def _deploy_text(*, byos: bool) -> str:
    from dango.cli.commands.deploy import _print_deploy_success

    with (
        patch("webbrowser.open"),
        patch("dango.cli.commands.deploy.console.print") as out,
    ):
        _print_deploy_success(
            url="http://1.2.3.4",
            ip="1.2.3.4",
            admin_email="a@example.com",
            warnings=[],
            byos=byos,
        )
    return "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)


def test_byos_summary_prints_the_backup_notice() -> None:
    text = _deploy_text(byos=True)
    assert "lost with" in text
    assert "dango remote backup download --from-server -o ./backup.tar.gz" in text
    assert "secrets" in text.lower() and "not in backups" in text.lower()


def test_do_summary_has_no_byos_download_notice() -> None:
    assert "./backup.tar.gz" not in _deploy_text(byos=False)


def test_helper_missing_message_points_to_installing_the_tool_and_upgrading() -> None:
    class _Ssh:
        def exec_command(self, *_a: object, **_k: object) -> object:
            raise AssertionError("not reached")

    with (
        patch("dango.platform.cloud.backup.helper_branch_active", return_value=False),
        pytest.raises(CloudProvisioningError) as err,
    ):
        cm.prebuild_images(_Ssh(), [])
    msg = str(err.value)
    assert "flock" in msg and "util-linux" in msg
    assert "dango remote upgrade" in msg
    assert "dango deploy" in msg  # only for the state where cloud.yml is gone


def test_unsupported_isolation_prints_loopback_unknown() -> None:
    status = IsolationStatus(state="unsupported", loopback="restricted", installed="1.0.13")
    with patch("dango.cli.commands.remote_isolation.get_isolation", return_value=status):
        result = invoke(IsoSSH(), ["status"])
    assert "Isolation: unsupported" in result.output
    assert "Loopback: unknown" in result.output
    assert "Loopback: restricted" not in result.output
