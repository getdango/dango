"""tests/factories/metabase_collection_fakes.py

Stateful in-memory fake of the Metabase collection REST surface, installed over ``requests``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch


def _response(status: int, body: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    return resp


class FakeMetabase:
    """Persistent collection state: the same instance backs repeated imports."""

    def __init__(self) -> None:
        self._next_id = 100
        self.collections: list[dict[str, Any]] = []
        self.item_counts: dict[int, int] = {}
        self.timelines: list[dict[str, Any]] = []
        self.mutations: list[tuple[str, str, Any]] = []
        self.deletes: list[str] = []
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.malformed_items: set[int] = set()
        self.dashboard_post_status = 200

    def add(
        self,
        name: str,
        parent: int | None = None,
        items: int = 0,
        cid: int | None = None,
    ) -> int:
        if cid is None:
            cid = self._next_id
            self._next_id += 1
        else:
            self._next_id = max(self._next_id, cid + 1)
        location = "/" if parent is None else self._location_of(parent) + f"{parent}/"
        self.collections.append({"id": cid, "name": name, "location": location, "archived": False})
        self.item_counts[cid] = items
        return cid

    def fail(self, method: str, path: str, *statuses: int) -> None:
        """Queue failure statuses for the next calls to ``method path`` (then recover)."""
        self.failures[(method, path)] = list(statuses)

    def active(self) -> list[dict[str, Any]]:
        return [c for c in self.collections if not c["archived"]]

    def archived_ids(self) -> list[int]:
        return sorted(c["id"] for c in self.collections if c["archived"])

    def active_named(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.active() if c["name"] == name]

    def _location_of(self, cid: int) -> str:
        for c in self.collections:
            if c["id"] == cid:
                return str(c["location"])
        return "/"

    def _children(self, cid: int) -> list[dict[str, Any]]:
        return [c for c in self.active() if c["location"].rstrip("/").endswith(f"/{cid}")]

    def _handle(self, method: str, url: str, **kwargs: Any) -> MagicMock:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0]
        queued = self.failures.get((method, path))
        if queued:
            status = queued.pop(0)
            return _response(status, {"message": "injected failure"})
        body = kwargs.get("json")
        if method == "GET" and path == "/api/collection":
            root = {"id": "root", "name": "Our analytics", "location": None}
            return _response(200, [root, *self.active()])
        match = re.fullmatch(r"/api/collection/(\d+)/items", path)
        if method == "GET" and match:
            cid = int(match.group(1))
            if cid in self.malformed_items:
                return _response(200, {"unexpected": True})
            rows = [{"model": "dashboard"}] * self.item_counts.get(cid, 0)
            rows += [{"model": "collection"}] * len(self._children(cid))
            # Real Metabase v0.62 answers an empty collection with {"total": null, "data": []}.
            return _response(200, {"data": rows, "total": len(rows) or None})
        if method == "GET" and path == "/api/timeline":
            return _response(200, self.timelines)
        if method == "GET" and path in ("/api/dashboard", "/api/card"):
            return _response(200, [])
        if method == "POST" and path == "/api/collection":
            self.mutations.append((method, path, body))
            parent = body.get("parent_id")
            return _response(200, {"id": self.add(body["name"], parent)})
        match = re.fullmatch(r"/api/collection/(\d+)", path)
        if method == "PUT" and match:
            self.mutations.append((method, path, body))
            cid = int(match.group(1))
            for c in self.collections:
                if c["id"] == cid and body.get("archived") is True:
                    c["archived"] = True
            return _response(200, {"id": cid, "archived": bool(body.get("archived"))})
        if method == "POST" and path == "/api/dashboard":
            return _response(self.dashboard_post_status, {"id": 900, "message": "boom"})
        if method == "DELETE":
            self.deletes.append(path)
            return _response(200, {})
        return _response(404, {"message": f"unhandled {method} {path}"})

    def collection_mutations(self) -> list[tuple[str, str, Any]]:
        return list(self.mutations)

    @contextmanager
    def installed(self) -> Iterator[FakeMetabase]:
        def make(method: str) -> Any:
            return lambda url, **kw: self._handle(method, url, **kw)

        with (
            patch("requests.get", side_effect=make("GET")),
            patch("requests.post", side_effect=make("POST")),
            patch("requests.put", side_effect=make("PUT")),
            patch("requests.delete", side_effect=make("DELETE")),
        ):
            yield self
