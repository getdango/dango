"""tests/unit/test_auth_disable_cloud.py

'dango auth disable' is refused on a cloud server and leaves auth.yml untouched.
On a local project it still works and prints an accurate message.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli


def _project(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    (dango_dir / "auth.yml").write_text("enabled: true\n")
    return tmp_path


@pytest.mark.unit
class TestAuthDisableCloud:
    def test_refused_on_cloud_and_auth_yml_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = _project(tmp_path)
        auth_yml = project_root / ".dango" / "auth.yml"
        before = auth_yml.read_bytes()
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = CliRunner().invoke(cli, ["auth", "disable"], input="y\n")
        assert result.exit_code != 0
        assert "cannot be disabled on a cloud server" in result.output
        assert auth_yml.read_bytes() == before

    def test_local_disable_works_with_accurate_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = _project(tmp_path)
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = CliRunner().invoke(cli, ["auth", "disable"], input="y\n")
        assert result.exit_code == 0
        from dango.auth.admin import is_auth_enabled

        assert is_auth_enabled(project_root) is False
        out = " ".join(result.output.split())
        assert "All endpoints are now public" not in out
        assert "this machine" in out
        assert "user management still requires login" in out
        assert "dango auth enable" in out
