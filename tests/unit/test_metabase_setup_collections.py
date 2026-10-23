"""tests/unit/test_metabase_setup_collections.py

First-setup creation of the Shared/Personal collections must be path-idempotent (K-34).
"""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _response(status_code: int, payload: object | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code, text="")
    response.json.return_value = payload
    return response


def _run_setup(tmp_path: Path, existing: list[dict]) -> MagicMock:
    """Run setup_metabase against a fake whose collection list is ``existing``."""
    from dango.visualization.metabase import setup_metabase

    session = MagicMock()
    session.get.side_effect = [
        _response(200, {"setup-token": "setup-token"}),
        _response(200, {"data": []}),
        _response(200, {"data": []}),
        _response(200, existing),
    ]
    session.post.side_effect = [
        _response(200),  # /api/setup
        _response(200, {"id": "initial-session"}),  # login after setup
        _response(200, {"id": 7}),  # create DuckDB connection
        _response(200, {"id": 50}),  # collection creates (at most two)
        _response(200, {"id": 51}),
        _response(200, {"id": "verified-session"}),
    ]
    session.put.return_value = _response(200)
    store = MagicMock()
    store.load_pending.return_value = None
    with ExitStack() as stack:
        stack.enter_context(
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-test")
        )
        stack.enter_context(
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.requests.Session", return_value=session)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.MetabaseCredentialStore", return_value=store)
        )
        stack.enter_context(patch("dango.visualization.metabase.write_metabase_metadata"))
        setup_metabase(tmp_path, "Test Project", "admin@example.com")
    return session


def _collection_posts(session: MagicMock) -> list[str]:
    return [
        call.kwargs["json"]["name"]
        for call in session.post.call_args_list
        if call.args and call.args[0].endswith("/api/collection")
    ]


@pytest.mark.unit
def test_retried_setup_does_not_duplicate_existing_root_collections(tmp_path: Path) -> None:
    existing = [
        {"id": "root", "name": "Our analytics", "location": None},
        {"id": 5, "name": "Shared", "location": "/"},
    ]
    assert _collection_posts(_run_setup(tmp_path, existing)) == ["Personal"]


@pytest.mark.unit
def test_nested_equal_name_does_not_suppress_root_creation(tmp_path: Path) -> None:
    existing = [
        {"id": 9, "name": "Shared", "location": "/4/"},
        {"id": 4, "name": "A", "location": "/"},
    ]
    assert _collection_posts(_run_setup(tmp_path, existing)) == ["Shared", "Personal"]
