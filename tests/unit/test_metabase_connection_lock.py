"""tests/unit/test_metabase_connection_lock.py

Metabase DuckDB connection lock: details helper, setup, existing-install catch-up, bounded wait.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tests.unit.test_metabase_site_url import (
    _NETWORK_CONFIG_GET_PROJECT_INFO,
    _mock_fresh_setup_session,
)

MB_URL = "http://localhost:3000"
LOCK_SQL = "SET enable_external_access=false;"
BASE_DETAILS: dict[str, Any] = {
    "database_file": "/data/warehouse.duckdb",
    "old_implicit_casting": True,
    "read_only": True,
}
_SYNC_REQUESTS = "dango.auth.metabase_sync.requests"


def _project(root: Path, db_id: int | None = 5, legacy_password: bool = True) -> Path:
    """Write the Metabase metadata of a set-up project (a legacy password avoids the keychain)."""
    admin = {"email": "a@b.co", **({"password": "pw"} if legacy_password else {})}
    meta: dict[str, Any] = {"metabase_url": MB_URL, "admin": admin}
    if db_id is not None:
        meta["database"] = {"id": db_id, "name": "Analytics"}
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "metabase.yml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    return root


def _resp(status: int, data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    return resp


def _fake_metabase(
    mock_req: MagicMock, details: dict[str, Any], login_status: int = 200, put_status: int = 200
) -> None:
    mock_req.post.return_value = _resp(login_status, {"id": "sess"})
    mock_req.get.return_value = _resp(200, {"id": 5, "name": "Analytics", "details": details})
    mock_req.put.return_value = _resp(put_status, {"id": 5})


def _marker(root: Path) -> Path:
    return root / ".dango" / "state" / "metabase_duckdb_lock_applied"


def _attempts(root: Path) -> Path:
    return root / ".dango" / "state" / "metabase_duckdb_lock_attempts"


def _refresh(root: Path, runs: int = 1) -> list[tuple[bool, str | None, str | None]]:
    """Run refresh_metabase_connection() ``runs`` times against faked Docker and login."""
    from dango.visualization.metabase import refresh_metabase_connection

    session = MagicMock()
    session.post.return_value = _resp(200, {"id": "refresh-session"})
    results = []
    for _ in range(runs):
        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch("dango.platform.docker.DockerManager") as docker_manager,
            patch(
                "dango.visualization.metabase.subprocess.run",
                side_effect=[MagicMock(stdout="dango-test-metabase-1\n"), MagicMock(returncode=0)],
            ),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
            patch("dango.visualization.metabase._apply_metabase_site_url_catchup"),
        ):
            docker_manager.return_value.compose_project_name = "dango-test"
            results.append(refresh_metabase_connection(root))
    return results


@pytest.mark.unit
def test_locked_duckdb_details_idempotent() -> None:
    from dango.visualization.metabase_connection_lock import (
        EXTERNAL_ACCESS_LOCK_SQL,
        locked_duckdb_details,
    )

    assert EXTERNAL_ACCESS_LOCK_SQL == LOCK_SQL
    original = dict(BASE_DETAILS)
    once = locked_duckdb_details(original)
    assert once == {**BASE_DETAILS, "init_sql": LOCK_SQL}
    assert original == BASE_DETAILS  # input untouched
    assert locked_duckdb_details(once) == once  # idempotent

    custom = locked_duckdb_details({**BASE_DETAILS, "init_sql": "INSTALL x; LOAD x;  "})
    assert custom["init_sql"] == f"INSTALL x; LOAD x; {LOCK_SQL}"
    assert locked_duckdb_details(custom) == custom
    assert custom["database_file"] == BASE_DETAILS["database_file"]


@pytest.mark.unit
def test_setup_details_include_lock(tmp_project_dir: Path) -> None:
    from dango.visualization.metabase import setup_metabase

    session = _mock_fresh_setup_session(
        [MagicMock(status_code=200, ok=True), MagicMock(status_code=200, ok=True)]
    )
    store = MagicMock()
    store.load_pending.return_value = None
    with (
        patch("dango.visualization.metabase.MetabaseCredentialStore", return_value=store),
        patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
        patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
        patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
        patch("dango.visualization.metabase.requests.Session", return_value=session),
        patch(_NETWORK_CONFIG_GET_PROJECT_INFO, return_value=None),
    ):
        result = setup_metabase(tmp_project_dir, "test-project", "admin@example.com")

    assert result["success"] is True
    creates = [c for c in session.post.call_args_list if c.args[0].endswith("/api/database")]
    assert len(creates) == 1
    assert creates[0].kwargs["json"]["details"] == {**BASE_DETAILS, "init_sql": LOCK_SQL}


@pytest.mark.unit
class TestEnsureDuckdbReadonly:
    @patch(_SYNC_REQUESTS)
    def test_ensure_duckdb_readonly_no_put_when_unchanged(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.auth.metabase_sync import ensure_duckdb_readonly

        _fake_metabase(mock_req, {**BASE_DETAILS, "init_sql": LOCK_SQL})
        assert ensure_duckdb_readonly(_project(tmp_path), MB_URL) is True
        mock_req.put.assert_not_called()

    @patch(_SYNC_REQUESTS)
    def test_ensure_duckdb_readonly_puts_merged_details(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.auth.metabase_sync import ensure_duckdb_readonly

        _fake_metabase(mock_req, {"database_file": "/data/warehouse.duckdb", "extra": 1})
        assert ensure_duckdb_readonly(_project(tmp_path), MB_URL) is True
        assert mock_req.put.call_count == 1
        assert mock_req.put.call_args.kwargs["json"]["details"] == {
            "database_file": "/data/warehouse.duckdb",
            "extra": 1,
            "read_only": True,
            "init_sql": LOCK_SQL,
        }


@pytest.mark.unit
class TestRefreshCatchup:
    @patch(_SYNC_REQUESTS)
    def test_refresh_applies_lock_once(self, mock_req: MagicMock, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS))

        results = _refresh(root, runs=2)

        assert [r[0] for r in results] == [True, True]
        assert mock_req.put.call_count == 1
        assert mock_req.put.call_args.kwargs["json"]["details"] == {
            **BASE_DETAILS,
            "init_sql": LOCK_SQL,
        }
        assert _marker(root).is_file()

    @patch(_SYNC_REQUESTS)
    def test_refresh_noop_when_already_locked(self, mock_req: MagicMock, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _fake_metabase(mock_req, {**BASE_DETAILS, "init_sql": LOCK_SQL})

        assert _refresh(root)[0][0] is True

        mock_req.put.assert_not_called()
        assert _marker(root).is_file()

    @patch(_SYNC_REQUESTS)
    def test_catchup_failure_is_non_fatal(self, mock_req: MagicMock, tmp_path: Path) -> None:
        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS), put_status=500)

        assert _refresh(root)[0] == (True, None, "refresh-session")
        assert not _marker(root).exists()


@pytest.mark.unit
class TestSyncTimeFailure:
    @patch(_SYNC_REQUESTS)
    def test_sync_failure_prints_one_warning(
        self, mock_req: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS), put_status=500)

        _refresh(root)

        captured = capsys.readouterr()
        warnings = [ln for ln in (captured.out + captured.err).splitlines() if "lock" in ln.lower()]
        assert len(warnings) == 1, captured

    @patch(_SYNC_REQUESTS)
    def test_sync_failures_do_not_use_up_the_start_attempts(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.platform.common.startup import metabase_startup_work_pending

        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS), put_status=500)
        _refresh(root, runs=4)

        assert not _attempts(root).exists()
        assert metabase_startup_work_pending(root) is True  # still waits at the next start


@pytest.mark.unit
def test_setup_update_path_puts_locked_details(tmp_project_dir: Path) -> None:
    from dango.visualization.metabase import setup_metabase

    session = _mock_fresh_setup_session(
        [MagicMock(status_code=200, ok=True), MagicMock(status_code=200, ok=True)]
    )
    existing = MagicMock(status_code=200)
    existing.json.return_value = {
        "data": [{"id": 7, "engine": "duckdb", "name": "test-project Analytics"}]
    }
    gets = list(session.get.side_effect)
    gets[1] = existing
    session.get.side_effect = gets
    session.put.side_effect = [MagicMock(status_code=200, ok=True) for _ in range(4)]
    store = MagicMock()
    store.load_pending.return_value = None
    with (
        patch("dango.visualization.metabase.MetabaseCredentialStore", return_value=store),
        patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
        patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
        patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
        patch("dango.visualization.metabase.requests.Session", return_value=session),
        patch(_NETWORK_CONFIG_GET_PROJECT_INFO, return_value=None),
    ):
        setup_metabase(tmp_project_dir, "test-project", "admin@example.com")

    updates = [
        c
        for c in session.put.call_args_list
        if c.args[0].endswith("/api/database/7") and "details" in c.kwargs["json"]
    ]
    assert len(updates) == 1
    assert updates[0].kwargs["json"]["details"] == {**BASE_DETAILS, "init_sql": LOCK_SQL}
    assert not [c for c in session.post.call_args_list if c.args[0].endswith("/api/database")]


@pytest.mark.unit
class TestRestoreRelock:
    """A restored Metabase DB can be unlocked while the state marker says applied."""

    @patch(_SYNC_REQUESTS)
    def test_refresh_relocks_unlocked_details_despite_marker(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        _marker(root).parent.mkdir(parents=True)
        _marker(root).write_text("applied\n")
        _fake_metabase(mock_req, dict(BASE_DETAILS))  # as after a restore: no init_sql

        assert _refresh(root)[0][0] is True

        assert mock_req.put.call_count == 1
        assert mock_req.put.call_args.kwargs["json"]["details"] == {
            **BASE_DETAILS,
            "init_sql": LOCK_SQL,
        }
        assert _marker(root).is_file()

    @patch(_SYNC_REQUESTS)
    def test_refresh_with_marker_and_locked_details_is_one_get_no_put(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        _marker(root).parent.mkdir(parents=True)
        _marker(root).write_text("applied\n")
        _fake_metabase(mock_req, {**BASE_DETAILS, "init_sql": LOCK_SQL})

        _refresh(root, runs=2)

        assert mock_req.get.call_count == 2  # one GET per refresh
        mock_req.put.assert_not_called()

    @patch(_SYNC_REQUESTS)
    def test_refresh_failure_with_marker_keeps_marker(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        _marker(root).parent.mkdir(parents=True)
        _marker(root).write_text("applied\n")
        _fake_metabase(mock_req, dict(BASE_DETAILS), put_status=500)

        _refresh(root)

        assert _marker(root).is_file()
        assert not _attempts(root).exists()

    def test_restore_clears_markers_and_next_start_relocks(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import restore_from_archive
        from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed
        from tests.factories.backup_shell import ShellSSH
        from tests.unit.test_backup_restore_service_user import _archive, _server

        project = tmp_path / "project"
        state = project / ".dango" / "state"
        state.mkdir(parents=True)
        _marker(project).write_text("applied\n")
        _attempts(project).write_text("2\n")
        (state / "other_marker").write_text("keep\n")
        mb_volume = tmp_path / "mb-volume"
        mb_volume.mkdir()
        ssh = ShellSSH(tmp_path / "shim")

        with _server(tmp_path, project, mb_volume):
            restore_from_archive(ssh, _archive(tmp_path))

        assert not _marker(project).exists()
        assert not _attempts(project).exists()
        assert (state / "other_marker").read_text() == "keep\n"  # only the lock files go

        _project(project)  # the restored metabase.yml of a set-up install
        with patch(_SYNC_REQUESTS) as mock_req:
            _fake_metabase(mock_req, dict(BASE_DETAILS))  # the restored H2 holds unlocked details
            apply_metabase_duckdb_lock_if_needed(project)
            assert mock_req.put.call_count == 1
        assert _marker(project).is_file()

    def test_restore_without_metabase_volume_keeps_markers(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import restore_from_archive
        from tests.factories.backup_shell import ShellSSH
        from tests.unit.test_backup_restore_service_user import _archive, _server

        project = tmp_path / "project"
        _marker(project).parent.mkdir(parents=True)
        _marker(project).write_text("applied\n")
        mb_volume = tmp_path / "mb-volume"
        mb_volume.mkdir()
        ssh = ShellSSH(tmp_path / "shim")

        with (
            _server(tmp_path, project, mb_volume),
            patch("dango.platform.cloud.backup._get_metabase_volume_path", return_value=None),
        ):
            restore_from_archive(ssh, _archive(tmp_path), require_metabase_volume=False)

        assert _marker(project).is_file()  # the H2 (and its lock) was not replaced


@pytest.mark.unit
class TestStartCatchup:
    @patch(_SYNC_REQUESTS)
    def test_start_applies_lock_once_without_sync(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed

        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS))

        apply_metabase_duckdb_lock_if_needed(root)
        assert mock_req.get.call_count == 1
        assert mock_req.put.call_count == 1
        assert _marker(root).is_file()

        mock_req.reset_mock()
        apply_metabase_duckdb_lock_if_needed(root)
        assert mock_req.mock_calls == []  # next start makes no Metabase call

    @patch(_SYNC_REQUESTS)
    def test_start_lock_failure_non_fatal_no_retry(
        self, mock_req: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed

        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS), login_status=401)

        apply_metabase_duckdb_lock_if_needed(root)

        assert mock_req.post.call_count == 1  # one login attempt, no retry loop
        mock_req.put.assert_not_called()
        assert not _marker(root).exists()
        assert _attempts(root).read_text().strip() == "1"
        captured = capsys.readouterr()
        assert len((captured.out + captured.err).strip().splitlines()) == 1

    @patch(_SYNC_REQUESTS)
    def test_start_skips_everything_when_metabase_not_set_up(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed

        apply_metabase_duckdb_lock_if_needed(_project(tmp_path, db_id=None))

        assert mock_req.mock_calls == []

    def test_start_does_not_wait_when_metabase_not_set_up(self, tmp_path: Path) -> None:
        from dango.platform.common.startup import metabase_startup_work_pending

        assert metabase_startup_work_pending(tmp_path) is False  # no metadata at all
        _project(tmp_path, db_id=None, legacy_password=False)
        assert metabase_startup_work_pending(tmp_path) is False  # no connection recorded yet
        _project(tmp_path, db_id=5, legacy_password=False)
        assert metabase_startup_work_pending(tmp_path) is True  # positive control
        _marker(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _marker(tmp_path).write_text("done\n")
        assert metabase_startup_work_pending(tmp_path) is False  # applied

    def test_start_stops_waiting_after_three_failed_attempts(self, tmp_path: Path) -> None:
        from dango.platform.common.startup import metabase_startup_work_pending

        _project(tmp_path, legacy_password=False)
        _attempts(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _attempts(tmp_path).write_text("2\n")
        assert metabase_startup_work_pending(tmp_path) is True
        _attempts(tmp_path).write_text("3\n")
        assert metabase_startup_work_pending(tmp_path) is False

    @patch(_SYNC_REQUESTS)
    def test_failed_attempts_are_counted_and_still_tried_each_start(
        self, mock_req: MagicMock, tmp_path: Path
    ) -> None:
        from dango.platform.common.startup import apply_metabase_duckdb_lock_if_needed

        root = _project(tmp_path)
        _fake_metabase(mock_req, dict(BASE_DETAILS), login_status=401)
        for _ in range(4):
            apply_metabase_duckdb_lock_if_needed(root)

        assert mock_req.post.call_count == 4  # one fast attempt per start, never a loop
        assert _attempts(root).read_text().strip() == "4"
