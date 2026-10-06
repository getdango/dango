"""tests/unit/test_dashboard_provision_credentials.py

Credential resolution in `dango dashboard provision` (1.0.13-T4, C10): the command uses the
project's stored Metabase admin credential and never prompts; explicit flags override it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.dashboard import dashboard

_OK = {
    "success": True,
    "dashboard_id": 1,
    "dashboard_url": "http://x",
    "cards_created": [],
    "errors": [],
}
_STORED = ("admin@stored.test", "stored-secret-pw")


@pytest.fixture
def provision(tmp_path: Path) -> Iterator[MagicMock]:
    """Patch every collaborator of the command; yield the fake ``provision_dashboard``."""
    fake = MagicMock(return_value=dict(_OK))
    with (
        patch("dango.visualization.provision_dashboard", fake),
        patch("dango.utils.pipeline_health.materialize_pipeline_health"),
        patch("dango.visualization.metabase.refresh_metabase_connection"),
    ):
        yield fake


def _run(tmp_path: Path, *args: str) -> Result:
    # No ``input=``: a surviving password prompt would abort the invocation.
    return CliRunner().invoke(dashboard, ["provision", *args], obj={"project_root": tmp_path})


def _creds(value: Any) -> Any:
    return patch("dango.security.metabase_config.load_metabase_admin_credentials", value)


def _meta(value: Any) -> Any:
    return patch("dango.security.metabase_config.load_metabase_metadata", value)


@pytest.mark.unit
class TestProvisionCredentialResolution:
    def test_provision_uses_stored_credential_without_prompt(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        with _creds(MagicMock(return_value=_STORED)):
            result = _run(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Password:" not in result.output
        kwargs = provision.call_args.kwargs
        assert kwargs["username"] == _STORED[0]
        assert kwargs["password"] == _STORED[1]
        assert _STORED[1] not in result.output

    def test_provision_password_flag_uses_metadata_email(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        loader = MagicMock()
        with (
            _creds(loader),
            _meta(MagicMock(return_value={"admin": {"email": "meta@x.test"}})),
        ):
            result = _run(tmp_path, "--password", "P")
        assert result.exit_code == 0, result.output
        loader.assert_not_called()
        kwargs = provision.call_args.kwargs
        assert (kwargs["username"], kwargs["password"]) == ("meta@x.test", "P")

    def test_provision_explicit_username_and_password(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        loader = MagicMock()
        with _creds(loader):
            result = _run(tmp_path, "--username", "U@x.test", "--password", "P")
        assert result.exit_code == 0, result.output
        loader.assert_not_called()
        kwargs = provision.call_args.kwargs
        assert (kwargs["username"], kwargs["password"]) == ("U@x.test", "P")

    def test_provision_aborts_when_no_stored_credential(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        with _creds(MagicMock(return_value=None)):
            result = _run(tmp_path)
        assert result.exit_code != 0
        assert "No Metabase admin credential is stored" in result.output
        provision.assert_not_called()

    def test_provision_aborts_when_credential_unreadable(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        with _creds(MagicMock(side_effect=RuntimeError("store is malformed"))):
            result = _run(tmp_path)
        assert result.exit_code != 0
        assert "Could not read the stored Metabase credential" in result.output
        assert "store is malformed" in result.output
        assert "dango metabase repair-admin" in result.output
        provision.assert_not_called()

    def test_provision_username_without_password_must_match_stored(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        with _creds(MagicMock(return_value=_STORED)):
            result = _run(tmp_path, "--username", "someone@else.test")
        assert result.exit_code != 0
        assert "differs from the project's Metabase admin" in result.output
        assert _STORED[1] not in result.output
        provision.assert_not_called()
        # A matching username (case-insensitive) is accepted and uses the stored password.
        with _creds(MagicMock(return_value=_STORED)):
            ok = _run(tmp_path, "--username", "ADMIN@stored.test")
        assert ok.exit_code == 0, ok.output
        assert provision.call_args.kwargs["password"] == _STORED[1]

    def test_provision_password_without_any_known_email_aborts(
        self, tmp_path: Path, provision: MagicMock
    ) -> None:
        with _meta(MagicMock(return_value=None)):
            result = _run(tmp_path, "--password", "P")
        assert result.exit_code != 0
        assert "Cannot tell which Metabase admin to log in as" in result.output
        provision.assert_not_called()

    def test_provision_auth_failure_points_to_repair_admin(self, tmp_path: Path) -> None:
        with (
            _creds(MagicMock(return_value=_STORED)),
            patch("dango.utils.pipeline_health.materialize_pipeline_health"),
            patch("dango.visualization.metabase.refresh_metabase_connection"),
            patch(
                "dango.visualization.metabase._metabase_login", side_effect=OSError("401")
            ) as login,
        ):
            # Real provision_dashboard + provisioner: a rejected login is attempted exactly once.
            result = _run(tmp_path)
        assert result.exit_code != 0
        output = " ".join(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", result.output).split())
        assert "Authentication failed" in output
        assert "dango metabase repair-admin" in output
        assert login.call_count == 1
