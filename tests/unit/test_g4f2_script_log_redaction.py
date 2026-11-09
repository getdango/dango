"""tests/unit/test_g4f2_script_log_redaction.py

The script run log page applies the same known-secret and pattern redaction as /api/logs to
stdout, stderr and the run metadata before rendering them to any role.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.exceptions import AuthorizationError
from dango.web.routes.scripts import router
from dango.web.routes.scripts_helpers import _get_log_dir

_ENV_CANARY = "canary-env-value-8d3f1a"
_TOML_CANARY = "canary-toml-value-77c2e9"
_PATTERN_CANARY = "pat-canary-5512ab90cd"


def _client(root: Path, role: Role) -> TestClient:
    app = FastAPI()
    app.state.project_root = root
    user = User(email="v@example.com", role=role, is_active=True)

    @app.middleware("http")
    async def inject_user(request: Any, call_next: Any) -> Any:
        request.state.user = user
        return await call_next(request)

    @app.exception_handler(AuthorizationError)
    async def forbidden(request: Any, exc: AuthorizationError) -> Any:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _run(root: Path, run_id: str = "run-1") -> None:
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "etl.py").write_text("print('x')")
    (root / ".env").write_text(f"WAREHOUSE_PASSWORD={_ENV_CANARY}\n")
    (root / ".dlt").mkdir()
    (root / ".dlt" / "secrets.toml").write_text(f'[sources.api]\nkey = "{_TOML_CANARY}"\n')
    log_dir = _get_log_dir(root) / run_id
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.txt").write_text(f"connected with {_ENV_CANARY}\nkey is {_TOML_CANARY}\n")
    (log_dir / "stderr.txt").write_text(
        f"Traceback: boom\nrequests.get('https://x.example/?token={_PATTERN_CANARY}')\n"
        f"password={_ENV_CANARY}\n"
    )
    (log_dir / "meta.json").write_text(
        json.dumps({"status": "failed", "exit_code": 1, "error": f"failed using {_TOML_CANARY}"})
    )


@pytest.mark.unit
class TestScriptLogPageRedaction:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR, Role.ADMIN])
    def test_canaries_are_not_rendered_for_any_role(self, tmp_path: Path, role: Role) -> None:
        _run(tmp_path)
        with patch("dango.web.routes.scripts.get_project_root", return_value=tmp_path):
            resp = _client(tmp_path, role).get("/scripts/etl.py/logs/run-1")
        assert resp.status_code == 200
        for canary in (_ENV_CANARY, _TOML_CANARY, _PATTERN_CANARY):
            assert canary not in resp.text
        assert "[REDACTED]" in resp.text
        assert "Traceback: boom" in resp.text

    def test_plain_output_is_unchanged(self, tmp_path: Path) -> None:
        _run(tmp_path)
        (_get_log_dir(tmp_path) / "run-1" / "stdout.txt").write_text("loaded 120 rows\n")
        with patch("dango.web.routes.scripts.get_project_root", return_value=tmp_path):
            resp = _client(tmp_path, Role.VIEWER).get("/scripts/etl.py/logs/run-1")
        assert "loaded 120 rows" in resp.text
