"""tests/integration/test_source_credential_binding_e2e.py

End to end: an agent-created source must not deliver an existing credential to the host it names.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.registry import get_source_metadata
from dango.ingestion.sources.setup_schema import compute_env_var_name

TOKEN = "value-belonging-to-another-service"


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:
        _Recorder.seen.append(self.headers.get("Authorization", ""))
        body = json.dumps([{"id": 1}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def listener() -> Iterator[str]:
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_agent_created_rest_api_source_does_not_send_existing_token(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch, listener: str
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    meta = get_source_metadata("rest_api") or {}
    token_param = next(p for p in meta["optional_params"] if p["name"] == "auth_token_env")
    env_name = compute_env_var_name(token_param, "foo")
    (root / ".env").write_text(f"{env_name}={TOKEN}\n")
    monkeypatch.delenv(env_name, raising=False)

    try:
        r = mcp_sources.create_source(
            "rest_api",
            "foo",
            {"base_url": listener, "endpoints": [{"path": "items", "name": "items"}]},
        )
        source = load_config(root).sources.get_source("foo")
        if "error" not in r and source is not None:
            from dango.ingestion.dlt_runner import run_sync

            run_sync(root, [source], skip_dbt=True)
    finally:
        os.environ.pop(env_name, None)  # run_sync loads .env into the process environment

    assert not any(TOKEN in header for header in _Recorder.seen), _Recorder.seen
    assert "error" in r, r
