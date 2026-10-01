"""tests/unit/test_env_backup_security.py

Tests that .env backups (previous credentials) are written owner-only and are
covered by the sensitive-artifact gitignore patterns.
"""

import shutil
import subprocess
import sys

import pytest

from dango.cli.env_helpers import create_env_template
from dango.config.credentials import (
    SENSITIVE_ARTIFACT_GITIGNORE_PATTERNS,
    ensure_sensitive_artifact_gitignores,
)

VARS = [{"name": "NEW_DUMMY_VAR", "display_name": "Dummy"}]


@pytest.mark.unit
class TestEnvBackupSecurity:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_env_backup_written_owner_only(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DUMMY_SECRET=dummy-value\n")

        create_env_template(env_file, VARS, backup=True)

        backup = tmp_path / ".env.env.backup"
        assert backup.read_text() == "DUMMY_SECRET=dummy-value\n"
        assert backup.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_preexisting_wide_backup_is_tightened(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DUMMY_SECRET=dummy-value\n")
        backup = tmp_path / ".env.env.backup"
        backup.write_text("old")
        backup.chmod(0o644)

        create_env_template(env_file, VARS, backup=True)

        assert backup.read_text() == "DUMMY_SECRET=dummy-value\n"
        assert backup.stat().st_mode & 0o777 == 0o600

    def test_env_backup_pattern_in_sensitive_list(self):
        assert ".env*.backup" in SENSITIVE_ARTIFACT_GITIGNORE_PATTERNS

    @pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
    def test_gitignore_hardening_ignores_env_backups(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        ensure_sensitive_artifact_gitignores(tmp_path)

        for name in (".env.env.backup", ".env.backup"):
            result = subprocess.run(
                ["git", "check-ignore", name], cwd=tmp_path, capture_output=True
            )
            assert result.returncode == 0, name

    def test_no_backup_written_when_backup_false(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DUMMY_SECRET=dummy-value\n")

        create_env_template(env_file, VARS, backup=False)

        assert not (tmp_path / ".env.env.backup").exists()

    def test_gitignore_hardening_is_idempotent(self, tmp_path):
        assert ensure_sensitive_artifact_gitignores(tmp_path) is True
        assert ensure_sensitive_artifact_gitignores(tmp_path) is False
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines.count(".env*.backup") == 1
