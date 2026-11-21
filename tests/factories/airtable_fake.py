"""tests/factories/airtable_fake.py

Stand-in for the `pyairtable` client (the Airtable connector's client boundary) serving metadata and
records in the shapes Airtable's REST API returns, so the connector runs with no network or token.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

CONNECTOR = "dango.ingestion.dlt_sources.airtable"


def airtable_table(
    name: str,
    primary: str,
    other_fields: tuple[str, ...] = ("Role",),
    table_id: str = "tblAAAAAAAAAAAAAA",
) -> dict[str, Any]:
    """Table metadata as returned by GET /meta/bases/{base}/tables (primaryFieldId + fields)."""
    fields = [{"id": "fldPRIMARY", "name": primary, "type": "singleLineText"}]
    fields += [
        {"id": f"fld{i:03d}", "name": n, "type": "singleLineText"}
        for i, n in enumerate(other_fields)
    ]
    return {"id": table_id, "name": name, "primaryFieldId": "fldPRIMARY", "fields": fields}


def airtable_record(rec_id: str, **fields: Any) -> dict[str, Any]:
    """A record as Airtable returns it: id and createdTime at top level, user columns under `fields`."""
    return {"id": rec_id, "createdTime": "2024-03-01T10:00:00.000Z", "fields": fields}


def _fake_module(
    tables: list[dict[str, Any]], records: dict[str, list[dict[str, Any]]]
) -> types.ModuleType:
    class _Table:
        def __init__(self, table_id: str) -> None:
            self._records = records.get(table_id, [])

        def iterate(self, **_kw: Any) -> Iterator[list[dict[str, Any]]]:
            # pyairtable yields one list of records per API page
            if self._records:
                yield list(self._records)

    class _Api:
        def __init__(self, _token: str) -> None:
            pass

        def build_url(self, path: str) -> str:
            return "https://api.airtable.com/v0/" + path

        def request(self, method: str, url: str, **_kw: Any) -> dict[str, Any]:
            assert method == "GET" and url.endswith("/tables"), (method, url)
            return {"tables": tables}

        def table(self, _base_id: str, table_id: str) -> _Table:
            return _Table(table_id)

    module = types.ModuleType("pyairtable")
    module.__dict__["Api"] = _Api
    return module


@contextmanager
def airtable_source(
    tables: list[dict[str, Any]], records: dict[str, list[dict[str, Any]]]
) -> Iterator[Any]:
    """Yield the connector's source built against the fake client (module state restored on exit)."""
    saved = {k: sys.modules.get(k) for k in ("pyairtable", CONNECTOR)}
    sys.modules["pyairtable"] = _fake_module(tables, records)
    sys.modules.pop(CONNECTOR, None)
    try:
        module = importlib.import_module(CONNECTOR)
        yield module.airtable_source(
            base_id="appBBBBBBBBBBBBBB", table_names=None, access_token="pat_fake"
        )
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
