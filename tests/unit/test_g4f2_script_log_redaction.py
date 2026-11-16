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


def _history_entry(root: Path) -> None:
    from dango.web.routes.scripts_helpers import _get_history_file

    path = _get_history_file(root, "etl.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "run_id": "r1",
        "status": "failed",
        "exit_code": 1,
        "error": f"boom password {_ENV_CANARY} and token={_PATTERN_CANARY}",
    }
    path.write_text(json.dumps(entry) + "\n")


@pytest.mark.unit
class TestScriptJsonRoutesRedactHistory:
    @pytest.mark.parametrize(
        "path", ["/api/scripts/etl.py/history", "/api/scripts/etl.py", "/api/scripts"]
    )
    def test_stored_history_errors_are_redacted_for_a_viewer(
        self, tmp_path: Path, path: str
    ) -> None:
        _run(tmp_path)
        _history_entry(tmp_path)
        with patch("dango.web.routes.scripts_api.get_project_root", return_value=tmp_path):
            resp = _client(tmp_path, Role.VIEWER).get(path)
        assert resp.status_code == 200
        assert "boom" in resp.text
        assert _ENV_CANARY not in resp.text
        assert _PATTERN_CANARY not in resp.text


@pytest.mark.unit
class TestScheduledScriptErrorIsRedactedWhereItIsBuilt:
    def _run(self, root: Path, *, stderr: str = "", popen_error: Exception | None = None) -> dict:
        from unittest.mock import MagicMock

        from dango.platform.scheduling.jobs import run_scheduled_script

        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "etl.py").write_text("print('x')")
        (root / ".env").write_text(f"WAREHOUSE_PASSWORD={_ENV_CANARY}\n")
        seen: dict = {"broadcast": [], "notify": [], "finish": [], "execution": []}
        started = MagicMock()
        started.proc.returncode = 1
        started.unisolated = False
        patches = [
            patch("dango.platform.scheduling.jobs._scheduler_service", None),
            patch("dango.platform.scheduling.jobs._try_record_start", return_value=1),
            patch(
                "dango.platform.scheduling.jobs._try_finish_record",
                side_effect=lambda *a, **k: seen["finish"].append((a, k)),
            ),
            patch(
                "dango.platform.scheduling.jobs._broadcast",
                side_effect=lambda m: seen["broadcast"].append(m),
            ),
            patch(
                "dango.platform.scheduling.jobs._notify",
                side_effect=lambda *a, **k: seen["notify"].append((a, k)),
            ),
            patch(
                "dango.platform.scheduling.jobs._log_execution_event",
                side_effect=lambda *a, **k: seen["execution"].append((a, k)),
            ),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
        ]
        if popen_error is not None:
            patches.append(
                patch("dango.platform.script_launch.start_script", side_effect=popen_error)
            )
        else:
            patches.append(patch("dango.platform.script_launch.start_script", return_value=started))
            patches.append(
                patch(
                    "dango.platform.script_launch.communicate_with_timeout",
                    return_value=("", stderr, False),
                )
            )
        for p in patches:
            p.start()
        try:
            run_scheduled_script("sched", script_path="etl.py", project_root=str(root))
        finally:
            patch.stopall()
        return seen

    @pytest.mark.parametrize("mode", ["stderr", "exception"])
    def test_every_error_sink_carries_redacted_text(self, tmp_path: Path, mode: str) -> None:
        message = f"boom password {_ENV_CANARY} and token={_PATTERN_CANARY}"
        if mode == "stderr":
            seen = self._run(tmp_path, stderr=message)
        else:
            seen = self._run(tmp_path, popen_error=OSError(message))
        dumped = json.dumps(seen, default=str)
        assert "boom" in dumped
        assert _ENV_CANARY not in dumped
        assert _PATTERN_CANARY not in dumped
        history = (tmp_path / ".dango" / "logs" / "scripts" / "etl.py.jsonl").read_text()
        assert "boom" in history
        assert _ENV_CANARY not in history
        assert _PATTERN_CANARY not in history
