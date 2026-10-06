"""tests/unit/test_cli_remote_repair_credentials.py

Contract tests for the remote Metabase schema-scan credential script.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_repair import (
    _metabase_schema_scan_command,
    _metabase_schema_scan_script,
    _metabase_secret_project_id_command,
    _validated_metabase_secret_project_id,
    remote_reset_metabase,
)
from dango.platform.cloud.ssh import CommandResult


@pytest.mark.unit
class TestRemoteRepairSchemaScanCredentials:
    """Ensure remote repair delegates credentials to the security boundary."""

    def test_schema_scan_script_uses_security_boundary_not_yaml(self) -> None:
        """The generated script must not read an admin password from project YAML."""
        script = _metabase_schema_scan_script()

        assert "from dango.security.metabase_config import" in script
        assert "load_metabase_metadata" in script
        assert "load_metabase_admin_credentials" in script
        assert 'admin.get("password")' not in script
        assert "yaml.safe_load" not in script
        assert "open('.dango/metabase.yml')" not in script
        assert "http://localhost:3000/api/database/{database_id}/sync_schema" in script

    def test_schema_scan_command_explicitly_uses_cloud_credential_store(self) -> None:
        """SSH schema scans must not rely on the dango-web systemd environment."""
        command = _metabase_schema_scan_command("/srv/dango/project")

        assert command.startswith("cd /srv/dango/project && ")
        assert "DANGO_CLOUD_MODE=true /srv/dango/venv/bin/python -c " in command
        assert 'admin.get("password")' not in command

    @pytest.mark.parametrize("cloud_mode", [None, "true"])
    def test_schema_scan_script_exits_before_importing_requests_without_credentials(
        self, tmp_path: Path, cloud_mode: str | None
    ) -> None:
        """Missing metadata or credentials exits cleanly without an HTTP login."""
        (tmp_path / "requests.py").write_text(
            'raise RuntimeError("requests must not be imported without credentials")\n',
            encoding="utf-8",
        )
        environment = os.environ.copy()
        repository_root = Path(__file__).resolve().parents[2]
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{repository_root}{os.pathsep}{existing_python_path}"
            if existing_python_path
            else str(repository_root)
        )
        if cloud_mode is None:
            environment.pop("DANGO_CLOUD_MODE", None)
        else:
            environment["DANGO_CLOUD_MODE"] = cloud_mode

        result = subprocess.run(
            [sys.executable, "-c", _metabase_schema_scan_script()],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr


@pytest.mark.unit
class TestRemoteResetMetabaseCredentials:
    """Ensure cloud reset cannot retain or broadly delete protected secrets."""

    @staticmethod
    def _result(stdout: str = "", *, success: bool = True) -> CommandResult:
        return CommandResult(stdout, "", 0 if success else 1)

    def _invoke_reset(
        self,
        project_id_output: str,
        *,
        lookup_success: bool = True,
        delete_success: bool = True,
    ):
        ssh = MagicMock()
        ssh.exec_command.side_effect = [
            self._result(project_id_output, success=lookup_success),
            self._result(),  # stop dango-web (only after the id validated)
            self._result(),  # compose down
            self._result(),  # remove metabase.yml
            self._result(success=delete_success),  # remove protected credential
            self._result(),  # restart dango-web
            self._result("ok\n"),  # health check
            self._result("Synced: 1, Created: 0\n"),  # user re-sync
        ]
        cloud_config = MagicMock(droplet_ip="203.0.113.1")

        with (
            patch(
                "dango.cli.commands.remote_mgmt._load_cloud_config_with_ip",
                return_value=(cloud_config, Path("/project")),
            ),
            patch("dango.cli.commands.remote_mgmt._make_ssh_manager", return_value=ssh),
            patch(
                "dango.platform.cloud.backup.get_remote_compose_project_name",
                return_value="dango-ab12cd34",
            ),
        ):
            result = CliRunner().invoke(remote_reset_metabase, input="reset\n")

        return result, ssh

    def test_confirmed_reset_removes_only_validated_secret_before_restart(self) -> None:
        project_id = "ab12cd34" * 4
        result, ssh = self._invoke_reset(f"{project_id}\n")

        assert result.exit_code == 0, result.output
        commands = [call.args[0] for call in ssh.exec_command.call_args_list]
        expected = (
            f"rm -f /srv/dango/secrets/metabase/{project_id}.json "
            f"/srv/dango/secrets/metabase/{project_id}.pending.json"
        )
        delete_index = commands.index(expected)
        restart_index = commands.index("systemctl start dango-web")
        assert delete_index < restart_index
        assert not any("rm -rf" in command for command in commands)

    def test_failed_secret_deletion_does_not_restart_dango_web(self) -> None:
        result, ssh = self._invoke_reset("ab12cd34" * 4 + "\n", delete_success=False)

        assert result.exit_code != 0
        assert "Could not remove this project's protected Metabase credential" in result.output
        assert "remains stopped" in result.output
        commands = [call.args[0] for call in ssh.exec_command.call_args_list]
        assert "systemctl start dango-web" not in commands

    @pytest.mark.parametrize(
        ("output", "lookup_success"),
        [
            ("", True),
            ("../../etc\n", True),
            ("AB12CD34" * 4 + "\n", True),
            ("ab12cd34" * 4 + "\n", False),
        ],
    )
    def test_invalid_or_failed_id_lookup_deletes_nothing_and_does_not_restart(
        self, output: str, lookup_success: bool
    ) -> None:
        result, ssh = self._invoke_reset(output, lookup_success=lookup_success)

        assert result.exit_code != 0
        assert "Nothing was stopped or removed" in result.output
        commands = [call.args[0] for call in ssh.exec_command.call_args_list]
        assert "systemctl start dango-web" not in commands
        assert not any("/srv/dango/secrets/metabase/" in command for command in commands)

    def test_identity_command_and_reset_output_do_not_include_credentials(self) -> None:
        command = _metabase_secret_project_id_command("/srv/dango/project")
        result, ssh = self._invoke_reset("ab12cd34" * 4 + "\n")

        assert "MetabaseCredentialStore" in command
        assert "password" not in command.lower()
        assert "password" not in result.output.lower()
        assert all(
            "password" not in call.args[0].lower() for call in ssh.exec_command.call_args_list
        )

    def test_confirmation_distinguishes_exported_and_metabase_only_work(self) -> None:
        result, _ssh = self._invoke_reset("ab12cd34" * 4 + "\n")

        assert "Project-exported dashboards can be re-imported" in result.output
        assert "Metabase-only dashboards and questions will be lost" in result.output

    @pytest.mark.parametrize("output", ["ab12cd34" * 4 + "\n", " " + "ab12cd34" * 4 + " \n"])
    def test_validated_project_id(self, output: str) -> None:
        assert _validated_metabase_secret_project_id(output) == "ab12cd34" * 4

    @pytest.mark.parametrize(
        "output", ["dango-" + "ab12cd34" * 4, "ab12cd34" * 3, "ab12cd34" * 3 + "ab12cd3g", ""]
    )
    def test_rejects_non_secret_path_project_id(self, output: str) -> None:
        assert _validated_metabase_secret_project_id(output) is None
