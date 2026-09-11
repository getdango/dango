"""tests/unit/test_metabase_site_url.py

1.0.8-W: Unit tests for setup_metabase()'s Metabase Site URL PUT
(PUT /api/setting/site-url) in dango/visualization/metabase.py.

Kept in its own file rather than folded into test_metabase_setup.py to avoid
pushing that already-near-the-500-line-limit file over the file-size-check
threshold. refresh_metabase_connection()'s Site URL refresh (the same fix
applied on every restart, not just at initial setup) is covered separately
in tests/unit/test_platform_startup.py::TestRefreshMetabaseConnection, next
to its other refresh_metabase_connection() tests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


def _mock_fresh_setup_session(put_side_effect: list[MagicMock]) -> MagicMock:
    """Build a requests.Session mock for the fresh-Metabase-setup happy path
    (session/properties -> setup -> login -> db list -> create db -> H2 list ->
    collection list/create). `put_side_effect` lets callers vary what the
    site-url / set-default PUT calls return without repeating the rest of the
    mock scaffolding.
    """
    import requests

    mock_session = MagicMock(spec=requests.Session)

    props_resp = MagicMock(status_code=200)
    props_resp.json.return_value = {"setup-token": "tok-abc"}
    setup_resp = MagicMock(status_code=200)
    login_resp = MagicMock(status_code=200)
    login_resp.json.return_value = {"id": "sess-xyz"}
    db_list_resp = MagicMock(status_code=200)
    db_list_resp.json.return_value = {"data": []}
    create_db_resp = MagicMock(status_code=200)
    create_db_resp.json.return_value = {"id": 7}
    h2_list_resp = MagicMock(status_code=200)
    h2_list_resp.json.return_value = {"data": []}
    coll_list_resp = MagicMock(status_code=200)
    coll_list_resp.json.return_value = []
    coll_create_resp = MagicMock(status_code=200)

    mock_session.get.side_effect = [props_resp, db_list_resp, h2_list_resp, coll_list_resp]
    mock_session.post.side_effect = [
        setup_resp,
        login_resp,
        create_db_resp,
        coll_create_resp,
        coll_create_resp,
    ]
    mock_session.put.side_effect = put_side_effect

    return mock_session


@pytest.mark.unit
class TestSetupMetabaseSiteUrl:
    """Test setup_metabase()'s Site URL PUT (1.0.8-W)."""

    def test_setup_metabase_sets_site_url(self, tmp_project_dir: Path) -> None:
        """setup_metabase() PUTs the /metabase/-suffixed, web-port-based Site URL
        right after login, before the DuckDB connection work. tmp_project_dir gives
        config.platform.port == 8800 (default, no platform: override in project.yml).
        """
        from dango.visualization.metabase import setup_metabase

        # Site-url PUT happens first (right after login), then the "set default
        # database" PUT further down in the DuckDB-connection block.
        mock_session = _mock_fresh_setup_session(
            [MagicMock(status_code=200), MagicMock(status_code=200)]
        )

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_project_dir, "test-project", "admin@example.com")

        assert result["success"] is True
        assert mock_session.put.call_args_list[0] == call(
            "http://localhost:3000/api/setting/site-url",
            headers={"X-Metabase-Session": "sess-xyz"},
            json={"value": "http://localhost:8800/metabase/"},
            timeout=10,
        )

    def test_setup_metabase_site_url_failure_does_not_block_setup(
        self, tmp_project_dir: Path
    ) -> None:
        """A non-200 from the site-url PUT must not stop the rest of setup_metabase()
        (DuckDB connection, credentials save) from completing."""
        from dango.visualization.metabase import setup_metabase

        mock_session = _mock_fresh_setup_session(
            [MagicMock(status_code=500), MagicMock(status_code=200)]
        )

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_project_dir, "test-project", "admin@example.com")

        assert result["success"] is True
        assert result["duckdb_connected"] is True
        assert result["credentials_saved"] is True
