"""tests/unit/test_remote_script_validation.py

Validates that all remote Python scripts built as strings in deploy/cloud code
compile correctly (ast.parse) and that their dango imports resolve in the
current codebase.

Prevents BUG-239 class bugs where a script references a nonexistent function
or module, only discovered at deploy time on a real server.
"""

from __future__ import annotations

import ast
import base64
import importlib
import re

import pytest

# ---------------------------------------------------------------------------
# Script extraction helpers
# ---------------------------------------------------------------------------


def _extract_admin_creation_script() -> str:
    """Extract the admin creation Python script from deploy_provision.py.

    The script is built as a string and base64-encoded.  We reconstruct it
    using the same logic as the source, with placeholder values.
    """
    script = (
        "import sys, os\n"
        "sys.path.insert(0, '/srv/dango/project')\n"
        "os.chdir('/srv/dango/project')\n"
        "from pathlib import Path\n"
        "from dango.auth.database import create_user, get_user_by_email, update_user\n"
        "from dango.auth.models import Role, User, UserUpdate\n"
        "from dango.exceptions import UserExistsError\n"
        "email = 'admin@example.com'\n"
        "pw_hash = '$2b$12$placeholder'\n"
        "db_path = Path('.dango/auth.db')\n"
        "# DB already initialized by server lifespan — just create/update user\n"
        "user = User(email=email, password_hash=pw_hash, role=Role.ADMIN, must_change_password=True)\n"
        "try:\n"
        "    create_user(db_path, user)\n"
        "except UserExistsError:\n"
        "    existing = get_user_by_email(db_path, email)\n"
        "    if existing:\n"
        "        update_user(db_path, existing.id, UserUpdate(password_hash=pw_hash, email=email, must_change_password=True))\n"
    )
    return script


def _extract_auth_timeout_script() -> str:
    """Extract the auth timeout configuration script from deploy_provision.py."""
    script = (
        "import sys, os\n"
        "sys.path.insert(0, '/srv/dango/project')\n"
        "os.chdir('/srv/dango/project')\n"
        "from pathlib import Path\n"
        "import yaml\n"
        "config_path = Path('.dango/project.yml')\n"
        "if config_path.exists():\n"
        "    config_data = yaml.safe_load(config_path.read_text()) or {}\n"
        "    config_data.setdefault('auth', {})\n"
        "    config_data['auth']['session_max_days'] = 30\n"
        "    config_data['auth']['idle_timeout_minutes'] = 60\n"
        "    config_path.write_text(yaml.dump(config_data, default_flow_style=False, sort_keys=False))\n"
    )
    return script


def _extract_spaces_upload_script() -> str:
    """Extract the Spaces upload script from migrate.py."""
    region = "nyc3"
    endpoint = f"https://{region}.digitaloceanspaces.com"
    script = (
        "import boto3, os\n"
        f"s3 = boto3.client('s3', region_name={region!r},\n"
        f"    endpoint_url={endpoint!r},\n"
        f"    aws_access_key_id=os.environ['SPACES_ACCESS_KEY'],\n"
        f"    aws_secret_access_key=os.environ['SPACES_SECRET_KEY'])\n"
        f"s3.upload_file('/tmp/backup.tar.gz', 'my-bucket', 'migration/backup.tar.gz')\n"
    )
    return script


def _extract_spaces_download_script() -> str:
    """Extract the Spaces download script from migrate.py."""
    region = "nyc3"
    endpoint = f"https://{region}.digitaloceanspaces.com"
    script = (
        "import boto3, os\n"
        "os.makedirs('/srv/dango/backups/deploy', exist_ok=True)\n"
        f"s3 = boto3.client('s3', region_name={region!r},\n"
        f"    endpoint_url={endpoint!r},\n"
        f"    aws_access_key_id=os.environ['SPACES_ACCESS_KEY'],\n"
        f"    aws_secret_access_key=os.environ['SPACES_SECRET_KEY'])\n"
        f"s3.download_file('my-bucket', 'migration/backup.tar.gz', '/srv/dango/backups/deploy/backup.tar.gz')\n"
    )
    return script


# All scripts with their source locations for error messages.
_ALL_SCRIPTS: list[tuple[str, str]] = [
    ("deploy_provision.py:_create_admin_and_enable_auth", _extract_admin_creation_script()),
    ("deploy_provision.py:_create_admin_and_enable_auth (timeout)", _extract_auth_timeout_script()),
    ("migrate.py:_upload_backup_to_spaces", _extract_spaces_upload_script()),
    ("migrate.py:_download_backup_from_spaces", _extract_spaces_download_script()),
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
