"""tests/factories/metabase_health_double.py

A local HTTP stand-in for Metabase's /api/health (ok, starting, error, hang, slow, huge bodies).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SECRET = "s3cr3t-token-9f2c"
SECRET_PATH = "/Users/someone/private-project/.dango/secrets"
_CHUNK = b"x" * 65536
_HUGE_BYTES = 256 * 1024 * 1024


class MetabaseDouble:
    """Serves ``GET /api/health`` on an ephemeral loopback port.

    Modes: ``ok`` (200), ``starting`` (503 initializing), ``error`` (500 with planted text),
    ``hang`` (never answers), ``slow_ok`` / ``slow_starting`` (answer after ``delay`` seconds),
    ``huge200`` / ``huge503`` (a 256 MB body; ``bytes_sent`` shows how much a client pulled).
    Change ``mode`` between requests; ``hits`` counts requests.
    """

    def __init__(self, mode: str, delay: float = 0.0) -> None:
        self.mode = mode
        self.delay = delay
        self.hits = 0
        self.bytes_sent = 0
        self.seen_headers: list[dict[str, str]] = []
        self.release = threading.Event()
        self._lock = threading.Lock()
        double = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with double._lock:
                    double.hits += 1
                    double.seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                mode = double.mode
                if mode == "hang":
                    double.release.wait(30)
                    return
                if mode.startswith("slow_"):
                    double.release.wait(double.delay)
                    mode = mode.removeprefix("slow_")
                if mode.startswith("huge"):
                    self._huge(200 if mode == "huge200" else 503)
                    return
                if mode == "ok":
                    status, body = 200, {"status": "ok"}
                elif mode == "starting":
                    status, body = 503, {"status": "initializing", "progress": 0.4}
                else:
                    status, body = 500, {"error": f"{SECRET} at {SECRET_PATH}"}
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _huge(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(_HUGE_BYTES))
                self.end_headers()
                try:
                    while double.bytes_sent < _HUGE_BYTES:
                        self.wfile.write(_CHUNK)
                        with double._lock:
                            double.bytes_sent += len(_CHUNK)
                except OSError:
                    return

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
