"""tests/unit/test_cli_remote_repair_credentials.py

Contract tests for the remote Metabase schema-scan credential script.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dango.cli.commands.remote_repair import (
    _metabase_schema_scan_command,
    _metabase_schema_scan_script,
)


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
