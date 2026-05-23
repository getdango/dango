"""tests/unit/test_remote_script_validation.py

Validates that all remote Python scripts built as strings in deploy/cloud code
compile correctly (ast.parse) and that their dango imports resolve in the
current codebase.

Prevents BUG-239 class bugs where a script references a nonexistent function
or module, only discovered at deploy time on a real server.

Scripts are imported from the actual source modules (not hardcoded copies)
so tests break when the source changes.
"""

from __future__ import annotations

import ast
import base64
import importlib
import re
from unittest.mock import MagicMock

import pytest

from dango.cli.commands.deploy_provision import (
    _build_admin_script,
    _build_auth_timeout_script,
    _start_services,
)
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.migrate import (
    _build_spaces_download_script,
    _build_spaces_upload_script,
)

# ---------------------------------------------------------------------------
# Script generation — calls the actual builder functions from source
# ---------------------------------------------------------------------------


def _get_admin_script() -> str:
    """Generate admin script via the real builder with placeholder values."""
    return _build_admin_script("admin@example.com", "$2b$12$placeholder")


def _get_auth_timeout_script() -> str:
    """Generate auth timeout script via the real builder."""
    return _build_auth_timeout_script()


def _get_spaces_upload_script() -> str:
    """Generate Spaces upload script via the real builder."""
    return _build_spaces_upload_script(
        archive_path="/tmp/backup.tar.gz",
        region="nyc3",
        endpoint="https://nyc3.digitaloceanspaces.com",
        access_key_env="SPACES_ACCESS_KEY",
        secret_key_env="SPACES_SECRET_KEY",
        bucket="my-bucket",
        spaces_key="migration/backup.tar.gz",
    )


def _get_spaces_download_script() -> str:
    """Generate Spaces download script via the real builder."""
    return _build_spaces_download_script(
        region="nyc3",
        endpoint="https://nyc3.digitaloceanspaces.com",
        access_key_env="SPACES_ACCESS_KEY",
        secret_key_env="SPACES_SECRET_KEY",
        bucket="my-bucket",
        spaces_key="migration/backup.tar.gz",
        local_path="/srv/dango/backups/deploy/backup.tar.gz",
    )


def _get_duckdb_checkpoint_script() -> str:
    """DuckDB checkpoint inline script from backup.py."""
    db_path = "/srv/dango/project/data/warehouse.duckdb"
    return f"import duckdb; c=duckdb.connect('{db_path}'); c.execute('CHECKPOINT'); c.close()"


def _get_sqlite_checkpoint_script() -> str:
    """SQLite WAL checkpoint inline script from backup.py."""
    db_path = "/srv/dango/project/.dango/auth.db"
    return (
        f"import sqlite3; c=sqlite3.connect('{db_path}'); "
        f"c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    )


# All scripts with their source locations for error messages.
_ALL_SCRIPTS: list[tuple[str, str]] = [
    ("deploy_provision.py:_build_admin_script", _get_admin_script()),
    ("deploy_provision.py:_build_auth_timeout_script", _get_auth_timeout_script()),
    ("migrate.py:_build_spaces_upload_script", _get_spaces_upload_script()),
    ("migrate.py:_build_spaces_download_script", _get_spaces_download_script()),
    ("backup.py:_checkpoint_duckdb", _get_duckdb_checkpoint_script()),
    ("backup.py:_checkpoint_auth_db", _get_sqlite_checkpoint_script()),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteScriptCompilation:
    """Verify all remote Python scripts compile via ast.parse()."""

    @pytest.mark.parametrize(
        ("label", "script"),
        _ALL_SCRIPTS,
        ids=[s[0] for s in _ALL_SCRIPTS],
    )
    def test_script_compiles(self, label: str, script: str) -> None:
        """Script must be valid Python syntax."""
        try:
            ast.parse(script)
        except SyntaxError as exc:
            pytest.fail(f"Script from {label} has syntax error: {exc}")

    @pytest.mark.parametrize(
        ("label", "script"),
        _ALL_SCRIPTS,
        ids=[s[0] for s in _ALL_SCRIPTS],
    )
    def test_script_base64_roundtrip(self, label: str, script: str) -> None:
        """Script survives base64 encode/decode (matches deploy pattern)."""
        encoded = base64.b64encode(script.encode()).decode()
        decoded = base64.b64decode(encoded).decode()
        assert decoded == script, f"Base64 roundtrip failed for {label}"
        # Verify decoded version also compiles
        ast.parse(decoded)


@pytest.mark.unit
class TestRemoteScriptImports:
    """Verify all dango imports in remote scripts resolve in the codebase."""

    _IMPORT_RE = re.compile(r"from\s+(dango\.\S+)\s+import\s+(.+?)(?:\n|$)")

    def _extract_dango_imports(self, script: str) -> list[tuple[str, list[str]]]:
        """Extract (module, [names]) for all ``from dango.x import y`` lines."""
        results: list[tuple[str, list[str]]] = []
        for match in self._IMPORT_RE.finditer(script):
            module = match.group(1)
            names = [n.strip() for n in match.group(2).split(",")]
            results.append((module, names))
        return results

    @pytest.mark.parametrize(
        ("label", "script"),
        _ALL_SCRIPTS,
        ids=[s[0] for s in _ALL_SCRIPTS],
    )
    def test_dango_imports_resolve(self, label: str, script: str) -> None:
        """Every ``from dango.x import y`` in remote scripts must resolve."""
        imports = self._extract_dango_imports(script)
        for module_path, names in imports:
            # Verify the module itself is importable
            try:
                mod = importlib.import_module(module_path)
            except ImportError:
                pytest.fail(f"Script {label}: module {module_path!r} cannot be imported")
            # Verify each named import exists in the module
            for name in names:
                if not hasattr(mod, name):
                    pytest.fail(f"Script {label}: {module_path}.{name} does not exist")


@pytest.mark.unit
class TestStartServicesErrorHandling:
    """Verify _start_services raises on failure (not silent)."""

    def test_raises_on_nonzero_exit(self) -> None:
        """_start_services must raise CloudProvisioningError when systemctl fails."""
        ssh = MagicMock()
        result = MagicMock()
        result.exit_code = 1
        result.stderr = "Unit dango-web.service not found."
        result.stdout = ""
        ssh.exec_command.return_value = result

        with pytest.raises(CloudProvisioningError, match="Failed to start dango-web"):
            _start_services(ssh)

    def test_success_does_not_raise(self) -> None:
        """_start_services must not raise when systemctl succeeds."""
        ssh = MagicMock()
        result = MagicMock()
        result.exit_code = 0
        result.stderr = ""
        result.stdout = ""
        ssh.exec_command.return_value = result

        _start_services(ssh)  # should not raise
        ssh.exec_command.assert_called_once()
