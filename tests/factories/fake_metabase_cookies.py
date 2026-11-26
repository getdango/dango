"""tests/factories/fake_metabase_cookies.py

A real local HTTP server that behaves like Metabase's session cookies, and records the Cookie
headers it receives.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

RESERVED_PORTS = {3000, 8081, 8800, 13310, 55432, 55433}
VALID_SESSIONS = {"user-b-sess", "user-c-sess", "bridged-sess", "fresh-sess"}


@dataclass
class Received:
    """One request as the upstream saw it."""

    method: str
    path: str
    who: str
    cookies: list[str]


class FakeMetabase:
    """A real HTTP server on a free local port that behaves like Metabase's cookie handling."""

    def __init__(self) -> None:
        self.received: list[Received] = []
        self.valid = set(VALID_SESSIONS)
        self.logins = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _record(self) -> tuple[str, dict[str, list[str]]]:
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                with outer._lock:
                    outer.received.append(
                        Received(
                            self.command,
                            parts.path,
                            query.get("who", [""])[0],
                            self.headers.get_all("Cookie") or [],
                        )
                    )
                return parts.path, query

            def _session(self) -> str | None:
                for header in self.headers.get_all("Cookie") or []:
                    for pair in header.split(";"):
                        name, _, value = pair.strip().partition("=")
                        if name == "metabase.SESSION":
                            return value
                return None

            def _send(
                self, status: int, body: bytes, cookies: list[str], body_delay: float = 0.0
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.flush()
                if body_delay:
                    time.sleep(body_delay)
                self.wfile.write(body)

            def do_POST(self) -> None:
                path, query = self._record()
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if path != "/api/session":
                    self._send(404, b"{}", [])
                    return
                with outer._lock:
                    outer.logins += 1
                    sid = f"sess-{outer.logins}"
                    outer.valid.add(sid)
                delay = float(query.get("slow", ["0"])[0])
                self._send(
                    200,
                    json.dumps({"id": sid}).encode(),
                    [
                        f"metabase.SESSION={sid}; Path=/; HttpOnly; SameSite=Lax",
                        "metabase.DEVICE=device-1; Path=/; HttpOnly; SameSite=Lax",
                        "metabase.TIMEOUT=alive; Path=/; Max-Age=1209600; SameSite=Lax",
                    ],
                    body_delay=delay,
                )

            def do_DELETE(self) -> None:
                """Logout: end the session and expire its cookie, as Metabase does."""
                self._record()
                session = self._session()
                with outer._lock:
                    outer.valid.discard(session or "")
                self._send(
                    204,
                    b"",
                    ["metabase.SESSION=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"],
                )

            def do_GET(self) -> None:
                _, query = self._record()
                time.sleep(float(query.get("delay", ["0"])[0]))
                session = self._session()
                if session is not None and session in outer.valid:
                    self._send(200, json.dumps({"session": session}).encode(), [])
                else:
                    self._send(401, b'{"message":"Unauthenticated"}', [])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        while self.server.server_address[1] in RESERVED_PORTS:
            self.server.server_close()
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def requests_for(self, who: str) -> list[Received]:
        return [r for r in self.received if r.who == who]
