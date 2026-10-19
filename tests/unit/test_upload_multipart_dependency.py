"""tests/unit/test_upload_multipart_dependency.py

The CSV upload endpoint needs python-multipart (never imported by name) to parse a file part.
"""

from collections.abc import Awaitable, Callable
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from dango.auth.models import Role, User
from dango.web.routes import upload

pytestmark = pytest.mark.unit


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _set_user(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.user = User(email="editor@example.com", role=Role.EDITOR)
        return await call_next(request)

    app.include_router(upload.router)
    return app


def test_python_multipart_is_installed() -> None:
    assert find_spec("python_multipart") is not None


def test_csv_upload_parses_multipart_file(tmp_path: Path) -> None:
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "sources.yml").write_text(
        yaml.safe_dump(
            {"sources": [{"name": "orders", "type": "csv", "csv": {"directory": "data/orders"}}]}
        )
    )
    with (
        patch.object(upload, "get_project_root", return_value=tmp_path),
        patch.object(upload, "append_log_entry"),
        patch.object(upload, "log_auth_event"),
        patch.object(upload.ws_manager, "broadcast", new=AsyncMock()),
    ):
        response = TestClient(_app()).post(
            "/api/sources/orders/upload-csv",
            files={"file": ("orders.csv", b"id,total\n1,10\n", "text/csv")},
        )

    assert response.status_code == 200, response.text
    assert (tmp_path / "data" / "orders" / "orders.csv").read_bytes() == b"id,total\n1,10\n"
