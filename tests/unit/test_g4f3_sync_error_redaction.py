"""tests/unit/test_g4f3_sync_error_redaction.py

Sync error text is redacted where it is created, when history is read back, and before it is
broadcast over the WebSocket, so a source credential in an exception never reaches a Viewer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dango.utils.sync_history import load_sync_history, save_sync_history_entry

_CANARY = "canary-sync-secret-4f9a1c"
_ERROR = f"HTTPError 401 for url https://api.example/v1/items?cursor={_CANARY}&page=2"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(f"SOURCE_API_VALUE={_CANARY}\n")
    (tmp_path / ".dango").mkdir(exist_ok=True)
    return tmp_path


def _no_canary(text: str) -> None:
    assert _CANARY not in text


@pytest.mark.unit
class TestSyncHistoryFile:
    def test_save_redacts_error_fields(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        save_sync_history_entry(
            root,
            "src",
            {"timestamp": "t", "status": "failed", "error_message": _ERROR, "error": _ERROR},
        )
        raw = (root / ".dango" / "history" / "src.json").read_text()
        _no_canary(raw)
        entry = json.loads(raw)[0]
        assert entry["status"] == "failed"
        assert "[REDACTED]" in entry["error_message"]

    def test_load_redacts_entries_written_before_the_fix(self, tmp_path: Path) -> None:
        root = _project(tmp_path)
        history_dir = root / ".dango" / "history"
        history_dir.mkdir()
        (history_dir / "src.json").write_text(
            json.dumps([{"timestamp": "t", "status": "failed", "error_message": _ERROR}])
        )
        entries = load_sync_history(root, "src")
        _no_canary(json.dumps(entries))
        assert entries[0]["status"] == "failed"


def _fake_config() -> Any:
    config = MagicMock()
    config.sources.get_source.return_value = MagicMock()
    return config


def _run_failing_sync(root: Path, run_sync_effect: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    from dango.platform.scheduling.sync_trigger import run_manual_sync
    from tests.unit.test_schedule_history_api import _create_db

    _create_db(root).rename(root / ".dango" / "scheduler.db")
    with (
        patch("dango.config.helpers.load_config", return_value=_fake_config()),
        patch("dango.oauth.validation.validate_before_sync"),
        patch("dango.ingestion.run_sync", **run_sync_effect),
    ):
        result = run_manual_sync(root, ["src"], write_progress=True, sync_id="abc")
    return result, root / ".dango" / "state" / "sync_status_abc.json"


@pytest.mark.unit
@pytest.mark.parametrize(
    "effect",
    [
        {"side_effect": RuntimeError(_ERROR)},
        {"return_value": {"failed_sources": [{"name": "src", "error": _ERROR}], "results": []}},
    ],
    ids=["exception", "failed_source"],
)
def test_manual_sync_failure_is_redacted_everywhere(tmp_path: Path, effect: dict[str, Any]) -> None:
    root = _project(tmp_path)
    result, status_file = _run_failing_sync(root, effect)

    assert result["status"] == "failed"
    _no_canary(json.dumps(result))
    # the progress file the web process turns into WebSocket events
    _no_canary(status_file.read_text())
    # the scheduler execution record read by the schedule history API
    conn = sqlite3.connect(str(root / ".dango" / "scheduler.db"))
    try:
        errors = [row[0] for row in conn.execute("SELECT error FROM execution_history")]
    finally:
        conn.close()
    assert errors
    for err in errors:
        _no_canary(err or "")
    # the per-source sync history (only written on the exception path)
    history = root / ".dango" / "history" / "src.json"
    if history.exists():
        _no_canary(history.read_text())


@pytest.mark.unit
def test_websocket_broadcast_redacts_error_text(tmp_path: Path) -> None:
    import asyncio

    from dango.web.routes.websocket import ConnectionManager

    root = _project(tmp_path)
    manager = ConnectionManager()
    ws = MagicMock()
    ws.send_json = AsyncMock()
    manager.active_connections.append(ws)

    message = {
        "event": "sync_failed",
        "source": "src",
        "message": f"Sync failed: {_ERROR}",
        "error": _ERROR,
        "nested": {"detail": _ERROR},
    }
    with patch("dango.web.helpers.get_project_root", return_value=root):
        asyncio.run(manager.broadcast(message, log=False))

    sent = ws.send_json.await_args.args[0]
    _no_canary(json.dumps(sent))
    assert sent["event"] == "sync_failed"
    assert sent["source"] == "src"


@pytest.mark.unit
def test_schedule_history_endpoints_redact_stored_errors(tmp_path: Path) -> None:
    """Rows written before the fix still hold raw text: the read endpoints redact them."""
    from fastapi.testclient import TestClient

    from tests.unit.test_schedule_history_api import _create_db, _make_schedule_app

    root = _project(tmp_path)
    db_path = _create_db(root)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO execution_history (schedule_name, started_at, status, error) "
        "VALUES ('nightly', '2026-11-01T00:00:00+00:00', 'failed', ?)",
        (_ERROR,),
    )
    conn.commit()
    conn.close()
    db_path.rename(root / ".dango" / "scheduler.db")

    client = TestClient(_make_schedule_app(root))
    with patch("dango.web.routes.schedules.get_project_root", return_value=root):
        recent = client.get("/api/schedules/history/recent")
        per_schedule = client.get("/api/schedules/nightly/history")

    assert recent.status_code == 200 and per_schedule.status_code == 200
    _no_canary(recent.text)
    _no_canary(per_schedule.text)
    assert recent.json()[0]["status"] == "failed"
    assert per_schedule.json()["items"][0]["status"] == "failed"
