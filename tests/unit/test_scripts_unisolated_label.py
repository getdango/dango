"""tests/unit/test_scripts_unisolated_label.py

The "Unisolated" badge for warehouse-write script runs (C6-P2b): shown on the
log page and wired into the scripts page rows only when the run's meta says so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from dango.auth.models import Role, User

_PATCH_SCRIPTS = "dango.web.routes.scripts"
_BADGE = "Unisolated: runs as the Dango service user, declared in scripts.yml"


def _make_app(tmp_path: Path) -> Any:
    from fastapi import FastAPI

    from dango.web.routes.scripts import router

    app = FastAPI()
    app.state.project_root = tmp_path
    user = User(id="admin-id", email="admin@test.com", role=Role.ADMIN, is_active=True)

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    app.include_router(router)
    return app


def _log_page(tmp_path: Path, script: str, meta_extra: dict[str, Any]) -> str:
    import dango.web.routes.scripts_helpers as helpers

    path = tmp_path / "scripts" / script
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('x')")
    log_dir = helpers._get_log_dir(tmp_path) / "run-1"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "meta.json").write_text(
        json.dumps({"run_id": "run-1", "script_name": script, "status": "success", **meta_extra})
    )
    client = TestClient(_make_app(tmp_path))
    with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
        resp = client.get(f"/scripts/{script}/logs/run-1")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.unit
class TestLogPageBadge:
    def test_badge_shown_for_unisolated_run(self, tmp_path: Path) -> None:
        assert _BADGE in _log_page(tmp_path, "hello.py", {"unisolated": True})

    def test_badge_absent_for_normal_run(self, tmp_path: Path) -> None:
        assert _BADGE not in _log_page(tmp_path, "hello.py", {})

    def test_badge_absent_when_flag_false(self, tmp_path: Path) -> None:
        assert _BADGE not in _log_page(tmp_path, "hello.py", {"unisolated": False})

    def test_script_name_stays_escaped(self, tmp_path: Path) -> None:
        html = _log_page(tmp_path, "<b>x</b>.py", {"unisolated": True})
        assert _BADGE in html
        assert "<b>x</b>" not in html
        assert "&lt;b&gt;x&lt;/b&gt;" in html


@pytest.mark.unit
class TestScriptsPageBadge:
    def test_rows_and_history_gate_badge_on_flag(self, tmp_path: Path) -> None:
        client = TestClient(_make_app(tmp_path))
        with patch(f"{_PATCH_SCRIPTS}.get_project_root", return_value=tmp_path):
            resp = client.get("/scripts")
        assert resp.status_code == 200
        assert resp.text.count(_BADGE) == 2
        assert "script.last_run.unisolated" in resp.text
        assert "run.unisolated" in resp.text
