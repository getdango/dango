"""tests/unit/test_check_service_via_http.py

The Windows HTTP health probe targets the configured Metabase port (1.0.13-T5).
"""

from unittest.mock import MagicMock, patch

from dango.web import helpers


def test_metabase_probe_uses_configured_url(tmp_path):
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "metabase.yml").write_text("metabase_url: http://localhost:3999\n")
    client = MagicMock()
    client.get.return_value.status_code = 200
    with (
        patch.object(helpers, "get_project_root", return_value=tmp_path),
        patch.object(helpers, "_get_health_check_client", return_value=client),
    ):
        assert helpers.check_service_via_http("metabase") == "running"
    client.get.assert_called_once_with("http://localhost:3999/api/health")
