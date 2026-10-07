"""tests/unit/test_env_write_security.py

Tests that create_env_template writes .env through an owner-only temp file,
cleans up on failure, and that the temp name is covered by the gitignore patterns.
"""

import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from dango.cli.env_helpers import create_env_template
from dango.config.credentials import (
    SENSITIVE_ARTIFACT_GITIGNORE_PATTERNS,
    ensure_sensitive_artifact_gitignores,
)

VARS = [{"name": "NEW_DUMMY_VAR", "display_name": "Dummy"}]


@pytest.mark.unit
class TestEnvWriteSecurity:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_new_env_file_is_owner_only(self, tmp_path):
        env_file = tmp_path / ".env"

        create_env_template(env_file, VARS, backup=False)

        assert env_file.exists()
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob(".env*.tmp"))

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_existing_wide_env_is_tightened(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DUMMY_SECRET=dummy-value\n")
        env_file.chmod(0o644)

        create_env_template(env_file, VARS, backup=False)

        assert env_file.stat().st_mode & 0o777 == 0o600
        content = env_file.read_text()
        assert content.startswith("DUMMY_SECRET=dummy-value")
        assert "NEW_DUMMY_VAR=" in content

    def test_failed_write_removes_temp_and_keeps_original(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_bytes(b"DUMMY_SECRET=dummy-value\n")

        with patch("pathlib.Path.replace", side_effect=OSError("boom")):
            with pytest.raises(Exception, match="Failed to update .env file"):
                create_env_template(env_file, VARS, backup=False)

        assert not list(tmp_path.glob(".env*.tmp"))
        assert env_file.read_bytes() == b"DUMMY_SECRET=dummy-value\n"

    def test_env_tmp_pattern_in_gitignore_patterns(self):
        assert ".env*.tmp" in SENSITIVE_ARTIFACT_GITIGNORE_PATTERNS

    @pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
    def test_gitignore_blocks_env_tmp(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        ensure_sensitive_artifact_gitignores(tmp_path)

        name = ".env.0123456789abcdef0123456789abcdef.tmp"
        result = subprocess.run(["git", "check-ignore", name], cwd=tmp_path, capture_output=True)
        assert result.returncode == 0
