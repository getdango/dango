"""tests/unit/test_config_route_metabase_url.py

/api/config reports the configured Metabase port (1.0.13-T5).
"""

import asyncio
from unittest.mock import patch

from dango.web.routes.config import get_config


def test_config_route_uses_configured_metabase_port(tmp_path):
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "project.yml").write_text(
        "project:\n  name: test\n  created_by: tester\n  purpose: test\nplatform:\n  metabase_port: 3555\n"
    )
    with patch("dango.web.routes.config.get_project_root", return_value=tmp_path):
        result = asyncio.run(get_config())
    assert result["metabase_url"] == "http://localhost:3555"
    assert set(result) == {
        "web_port",
        "web_url",
        "metabase_url",
        "dbt_docs_url",
        "api_url",
        "project_name",
        "organization",
    }
