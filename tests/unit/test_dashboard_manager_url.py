"""tests/unit/test_dashboard_manager_url.py

DashboardManager derives its Metabase URL from the project, not a hard-coded port (1.0.13-T5).
"""

from unittest.mock import patch

from dango.visualization.dashboard_manager import DashboardManager

_CREDS = "dango.visualization.dashboard_manager.load_metabase_admin_credentials"


@patch(_CREDS, return_value=None)
def test_default_url_comes_from_metabase_metadata(_creds, tmp_path):
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "metabase.yml").write_text("metabase_url: http://localhost:3999\n")
    assert DashboardManager(tmp_path).metabase_url == "http://localhost:3999"


@patch(_CREDS, return_value=None)
def test_default_url_comes_from_configured_port(_creds, tmp_path):
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "project.yml").write_text(
        "project:\n  name: test\n  created_by: tester\n  purpose: test\nplatform:\n  metabase_port: 3555\n"
    )
    assert DashboardManager(tmp_path).metabase_url == "http://localhost:3555"


@patch(_CREDS, return_value=None)
def test_explicit_url_wins(_creds, tmp_path):
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "metabase.yml").write_text("metabase_url: http://localhost:3999\n")
    assert DashboardManager(tmp_path, metabase_url="http://x:1/").metabase_url == "http://x:1"
