"""dango/visualization/collection_repair_api.py

Metabase collection HTTP adapter and the atomic repair report writer for collection_repair.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

import requests

from dango.visualization.collection_repair import CollectionApiError, RepairResult

REPORT_RELATIVE_PATH = Path(".dango") / "state" / "metabase_collection_repair.json"


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


class RequestsCollectionApi:
    """Metabase collection endpoints, verified against the pinned v0.62.x server."""

    def __init__(self, base_url: str, headers: dict[str, str], timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def list_collections(self) -> list[dict[str, Any]]:
        """Active collections; raises CollectionApiError on any non-200 or odd shape."""
        resp = requests.get(f"{self.base_url}/api/collection", headers=self.headers, timeout=10)
        if resp.status_code != 200:
            raise CollectionApiError(f"HTTP {resp.status_code}")
        body = resp.json()
        if not isinstance(body, list):
            raise CollectionApiError("unexpected response shape")
        return [item for item in body if isinstance(item, dict)]

    def count_items(self, collection_id: int) -> int | None:
        """Items in a collection (child collections included). None when unknown.

        An empty collection answers ``{"total": null, "data": []}``.
        """
        resp = requests.get(
            f"{self.base_url}/api/collection/{collection_id}/items",
            params={"limit": 1},
            headers=self.headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return None
        total = body.get("total")
        if body["data"]:
            return total if _is_int(total) and total >= len(body["data"]) else len(body["data"])
        return 0 if total is None or total == 0 else None

    def create(self, name: str, parent_id: int | None) -> int:
        """POST a new collection and return its integer ID."""
        resp = requests.post(
            f"{self.base_url}/api/collection",
            json={"name": name, "parent_id": parent_id},
            headers=self.headers,
            timeout=self.timeout,
        )
        if resp.status_code not in (200, 201):
            raise CollectionApiError(f"HTTP {resp.status_code}")
        new_id = resp.json().get("id")
        if not _is_int(new_id):
            raise CollectionApiError("response missing 'id'")
        return int(new_id)

    def archive(self, collection_id: int) -> None:
        """Archive via PUT archived=true (reversible; never DELETE)."""
        resp = requests.put(
            f"{self.base_url}/api/collection/{collection_id}",
            json={"archived": True},
            headers=self.headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise CollectionApiError(f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _previous_report_incomplete(path: Path) -> bool:
    try:
        return json.loads(path.read_text()).get("complete") is False
    except (OSError, ValueError):
        return False


def write_report(project_root: Path, result: RepairResult, dango_version: str) -> Path | None:
    """Atomically write the non-secret repair report when there is something to record."""
    path = project_root / REPORT_RELATIVE_PATH
    interesting = bool(result.archived_ids or result.ambiguous_ids or result.errors)
    if not interesting and not _previous_report_incomplete(path):
        return None
    payload = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "dango_version": dango_version,
        "export_fingerprint": result.fingerprint,
        "complete": result.complete,
        "kept_ids": result.kept_ids,
        "archived_ids": result.archived_ids,
        "ambiguous_ids": result.ambiguous_ids,
        "errors": result.errors,
        "restore_hint": 'PUT /api/collection/<id> with {"archived": false} restores an archived id',
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".collection_repair.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
