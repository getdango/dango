"""tests/integration/test_metabase_duckdb_lock.py

Real pinned Metabase and driver: the DuckDB connection is restricted to the warehouse file.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests

pytestmark = pytest.mark.integration

_ADMIN_EMAIL = "admin@dango-duckdb-lock.test"
_EDITOR_EMAIL = "editor@dango-duckdb-lock.test"
_EDITOR_PASSWORD = "Editor-Lock-Test-pw-77123!"  # throwaway credential for a disposable container
_LOCK_SQL = "SET enable_external_access=false;"
_DISABLED = "file system operations are disabled"
_FILE_READS = [
    "select * from read_text('/etc/hostname')",
    "COPY (select 1) TO '/metabase-data/lock-test.csv'",
    "select * from glob('/metabase-data/*')",
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Stack:
    """Handle on one real Dango project's Metabase container and its API sessions."""

    def __init__(self, root: Path, url: str, db_id: int, admin: tuple[str, str]) -> None:
        self.root, self.url, self.db_id, self.admin = root, url, db_id, admin

    def admin_headers(self) -> dict[str, str]:
        # The per-test keyring isolation hides the stored credential; log in with the cached one.
        resp = requests.post(
            f"{self.url}/api/session",
            json={"username": self.admin[0], "password": self.admin[1]},
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        return {"X-Metabase-Session": resp.json()["id"]}

    def editor_headers(self) -> dict[str, str]:
        resp = requests.post(
            f"{self.url}/api/session",
            json={"username": _EDITOR_EMAIL, "password": _EDITOR_PASSWORD},
            timeout=30,
        )
        assert resp.status_code == 200, resp.text
        return {"X-Metabase-Session": resp.json()["id"]}

    def native(self, headers: dict[str, str], sql: str) -> dict[str, Any]:
        resp = requests.post(
            f"{self.url}/api/dataset",
            headers=headers,
            json={"database": self.db_id, "type": "native", "native": {"query": sql}},
            timeout=60,
        )
        body: dict[str, Any] = resp.json()
        return body

    def details(self) -> dict[str, Any]:
        resp = requests.get(
            f"{self.url}/api/database/{self.db_id}", headers=self.admin_headers(), timeout=30
        )
        details: dict[str, Any] = resp.json()["details"]
        return details


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
    assert shutil.which("docker") is not None, "DANGO_RUN_DOCKER_TESTS=1 but docker is missing"

    import duckdb

    from dango.auth.metabase_sync import _mb_post, ensure_metabase_groups
    from dango.cli.init import ProjectInitializer, init_project
    from dango.config import ConfigLoader
    from dango.platform import DockerManager
    from dango.platform.common.startup import setup_metabase_if_needed, start_docker_services
    from tests.integration.docker_leak_support import teardown_and_check

    root = tmp_path_factory.mktemp("metabase-duckdb-lock")
    previous = os.environ.get("DANGO_ADMIN_EMAIL")
    os.environ["DANGO_ADMIN_EMAIL"] = _ADMIN_EMAIL
    manager = None
    try:
        init_project(root, skip_wizard=True)
        loader = ConfigLoader(root)
        config = loader.load_config()
        config.platform.port = _free_port()
        config.platform.metabase_port = _free_port()
        config.platform.dbt_docs_port = _free_port()
        loader.save_config(config)
        ProjectInitializer(root)._create_docker_compose(config)

        (root / "data").mkdir(exist_ok=True)
        con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
        con.execute("create table raw_t as select i as id, 'n' || i as name from range(1, 11) r(i)")
        con.execute("create view v_t as select id, name from raw_t")
        con.close()

        manager = DockerManager(root)
        start_docker_services(root)
        result = setup_metabase_if_needed(root, config.project.name, organization=None)
        assert result.get("success"), f"Metabase setup failed: {result}"

        from dango.security.metabase_config import (
            load_metabase_admin_credentials,
            load_metabase_metadata,
        )

        metadata = load_metabase_metadata(root) or {}
        admin_credentials = load_metabase_admin_credentials(root)
        assert admin_credentials is not None
        handle = _Stack(
            root, str(metadata["metabase_url"]), int(metadata["database"]["id"]), admin_credentials
        )

        # A native-query user shaped like the SSO Editor mapping.
        admin = handle.admin_headers()
        groups = ensure_metabase_groups(handle.url, root, admin["X-Metabase-Session"])
        assert groups is not None
        user = _mb_post(
            handle.url,
            admin["X-Metabase-Session"],
            "/api/user",
            {
                "first_name": "Ed",
                "last_name": "Itor",
                "email": _EDITOR_EMAIL,
                "password": _EDITOR_PASSWORD,
            },
        )
        assert user is not None
        member = _mb_post(
            handle.url,
            admin["X-Metabase-Session"],
            "/api/permissions/membership",
            {"group_id": groups["editors"], "user_id": user["id"]},
        )
        assert member is not None
        yield handle
    finally:
        try:
            if manager is not None:
                manager.stop_services()
        finally:
            try:
                if manager is not None:
                    teardown_and_check(root, manager.compose_project_name)
            finally:
                if previous is None:
                    os.environ.pop("DANGO_ADMIN_EMAIL", None)
                else:
                    os.environ["DANGO_ADMIN_EMAIL"] = previous


def test_metabase_blocks_file_access(stack: _Stack) -> None:
    assert stack.details().get("init_sql") == _LOCK_SQL
    editor = stack.editor_headers()
    for sql in _FILE_READS:
        body = stack.native(editor, sql)
        assert body.get("status") == "failed", f"{sql!r} was not blocked: {body}"
        assert _DISABLED in str(body.get("error")), body
    flag = stack.native(editor, "select current_setting('enable_external_access')")
    assert flag["data"]["rows"] == [[False]]


def test_metabase_normal_queries_still_work(stack: _Stack) -> None:
    admin = stack.admin_headers()
    editor = stack.editor_headers()
    assert stack.native(editor, "select count(*) from v_t")["data"]["rows"] == [[10]]

    meta = requests.get(
        f"{stack.url}/api/database/{stack.db_id}/metadata", headers=admin, timeout=60
    ).json()
    table_id = next(t["id"] for t in meta["tables"] if t["name"] == "v_t")
    query = {"source-table": table_id}
    card = requests.post(
        f"{stack.url}/api/card",
        headers=admin,
        json={
            "name": "lock test card",
            "display": "table",
            "visualization_settings": {},
            "dataset_query": {"database": stack.db_id, "type": "query", "query": query},
        },
        timeout=60,
    )
    assert card.status_code == 200, card.text
    card_id = card.json()["id"]
    dash = requests.post(
        f"{stack.url}/api/dashboard", headers=admin, json={"name": "lock test dash"}, timeout=60
    )
    dash_id = dash.json()["id"]
    placed = requests.put(
        f"{stack.url}/api/dashboard/{dash_id}",
        headers=admin,
        json={
            "dashcards": [
                {"id": -1, "card_id": card_id, "row": 0, "col": 0, "size_x": 8, "size_y": 6}
            ]
        },
        timeout=60,
    )
    assert placed.status_code == 200, placed.text
    dashcard_id = requests.get(
        f"{stack.url}/api/dashboard/{dash_id}", headers=admin, timeout=60
    ).json()["dashcards"][0]["id"]
    for path in (
        f"/api/card/{card_id}/query",
        f"/api/dashboard/{dash_id}/dashcard/{dashcard_id}/card/{card_id}/query",
    ):
        resp = requests.post(f"{stack.url}{path}", headers=admin, json={}, timeout=60)
        body = resp.json()
        assert body["status"] == "completed", body
        assert len(body["data"]["rows"]) == 10

    # Schema sync of a relation created after the lock still works.
    import duckdb

    con = duckdb.connect(str(stack.root / "data" / "warehouse.duckdb"))
    con.execute("create view v_after_lock as select * from raw_t where id < 4")
    con.close()
    sync = requests.post(
        f"{stack.url}/api/database/{stack.db_id}/sync_schema", headers=admin, timeout=60
    )
    assert sync.status_code == 200, sync.text


def test_startup_catchup_locks_an_existing_unlocked_connection(stack: _Stack) -> None:
    from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed

    admin = stack.admin_headers()
    row = requests.get(f"{stack.url}/api/database/{stack.db_id}", headers=admin, timeout=30).json()
    unlocked = {k: v for k, v in row["details"].items() if k != "init_sql"}
    row["details"] = unlocked
    put = requests.put(
        f"{stack.url}/api/database/{stack.db_id}", headers=admin, json=row, timeout=60
    )
    assert put.status_code == 200, put.text
    assert "init_sql" not in stack.details()
    editor = stack.editor_headers()
    assert stack.native(editor, _FILE_READS[0])["status"] == "completed"  # positive control

    marker = stack.root / ".dango" / "state" / "metabase_duckdb_lock_applied"
    marker.unlink(missing_ok=True)
    with patch(
        "dango.auth.metabase_sync.load_metabase_admin_credentials", return_value=stack.admin
    ):
        apply_metabase_duckdb_lock_if_needed(stack.root)

    assert marker.is_file()
    assert stack.details() == {**unlocked, "init_sql": _LOCK_SQL}
    assert _DISABLED in str(stack.native(stack.editor_headers(), _FILE_READS[0]).get("error"))
