"""tests/unit/test_metabase_reset.py

Unit tests for _reset_metabase_volume() in dango/visualization/metabase.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestResetMetabaseVolume:
    """Test _reset_metabase_volume helper."""

    @patch("dango.visualization.metabase.subprocess.run")
    @patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc12345")
    def test_success(self, mock_compose_name: MagicMock, mock_run: MagicMock, tmp_path: Path):
        from dango.visualization.metabase import _reset_metabase_volume

        mock_run.return_value = MagicMock(returncode=0)

        result = _reset_metabase_volume(tmp_path)

        assert result is True
        assert mock_run.call_count == 4

        # Verify the 4 subprocess calls in order
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["docker", "compose", "stop", "metabase"]
        assert calls[1][0][0] == ["docker", "compose", "rm", "-f", "metabase"]
        assert calls[2][0][0] == ["docker", "volume", "rm", "dango-abc12345_metabase-data"]
        assert calls[3][0][0] == ["docker", "compose", "up", "-d", "metabase"]

    @patch("dango.visualization.metabase.subprocess.run")
    @patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc12345")
    def test_volume_rm_failure_returns_false(
        self, mock_compose_name: MagicMock, mock_run: MagicMock, tmp_path: Path
    ):
        from dango.visualization.metabase import _reset_metabase_volume

        def _side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if "volume" in cmd and "rm" in cmd:
                result.returncode = 1
                result.stderr = b"volume is in use"
            return result

        mock_run.side_effect = _side_effect

        result = _reset_metabase_volume(tmp_path)

        assert result is False
        # Should NOT call docker compose up after failed volume rm
        assert mock_run.call_count == 3

    @patch("dango.visualization.metabase.subprocess.run")
    @patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc12345")
    def test_timeout_returns_false(
        self, mock_compose_name: MagicMock, mock_run: MagicMock, tmp_path: Path
    ):
        from dango.visualization.metabase import _reset_metabase_volume

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)

        result = _reset_metabase_volume(tmp_path)

        assert result is False

    @patch("dango.visualization.metabase.subprocess.run")
    @patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc12345")
    def test_compose_project_name_used(
        self, mock_compose_name: MagicMock, mock_run: MagicMock, tmp_path: Path
    ):
        from dango.visualization.metabase import _reset_metabase_volume

        mock_run.return_value = MagicMock(returncode=0)

        _reset_metabase_volume(tmp_path)

        mock_compose_name.assert_called_once_with(tmp_path)
        # Verify COMPOSE_PROJECT_NAME is in the env for compose commands
        for call in mock_run.call_args_list:
            if "compose" in call[0][0]:
                assert call[1]["env"]["COMPOSE_PROJECT_NAME"] == "dango-abc12345"
