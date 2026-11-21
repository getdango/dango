"""tests/factories/connector_contract.py

Factory for the vendored-connector primary-key contract tests: a fake HTTP transport (no sockets) and a
runner that loads synthetic records through real dlt into a temporary DuckDB, then checks the key contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import dlt
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

# (status, json body, extra headers)
Reply = tuple[int, Any, dict[str, str]]
Handler = Callable[[requests.PreparedRequest], Any]


class UnroutedRequest(RuntimeError):
    """A connector asked for a URL the test did not stub (so it would have gone to the network)."""


class FakeHttp:
    """Replaces the HTTP transport (`HTTPAdapter.send`) with canned JSON replies.

    Every `requests`-based client in the vendored connectors, including dlt's own retrying session, sends
    through this adapter, so patching it is the connectors' client boundary without touching their code.
    A URL with no route raises `UnroutedRequest`; nothing ever reaches a socket.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[re.Pattern[str], Handler]] = []
        self.calls: list[str] = []

    def route(self, pattern: str, reply: Any) -> FakeHttp:
        """Register `reply` (JSON body, `(status, body, headers)` tuple or callable) for URLs matching."""
        handler: Handler = reply if callable(reply) else (lambda _req, _r=reply: _r)
        self._routes.append((re.compile(pattern), handler))
        return self

    def _send(self, _adapter: HTTPAdapter, request: requests.PreparedRequest, **_kw: Any):
        url = request.url or ""
        self.calls.append(url)
        for pattern, handler in self._routes:
            if pattern.search(url):
                result = handler(request)
                status, body, headers = (
                    result if isinstance(result, tuple) and len(result) == 3 else (200, result, {})
                )
                response = requests.Response()
                response.status_code = status
                response._content = json.dumps(body).encode("utf-8")
                response.headers = CaseInsensitiveDict(
                    {"content-type": "application/json", **headers}
                )
                response.url = url
                response.request = request
                response.encoding = "utf-8"
                return response
        raise UnroutedRequest(f"no stub for {request.method} {url}")

    @contextmanager
    def active(self) -> Iterator[FakeHttp]:
        with patch.object(
            HTTPAdapter, "send", lambda adapter, req, **kw: self._send(adapter, req, **kw)
        ):
            yield self


def make_pipeline(root: Path, name: str = "contract") -> dlt.Pipeline:
    """A pipeline with the destination settings `DltPipelineRunner` uses (DuckDB file, named dataset)."""
    return dlt.pipeline(
        pipeline_name=name,
        pipelines_dir=str(root / "pipelines"),
        destination=dlt.destinations.duckdb(credentials=str(root / "warehouse.duckdb")),
        dataset_name="raw_contract",
    )


@dataclass(frozen=True)
class TableState:
    """What a loaded table looks like: its primary-key columns, disposition, columns and row count."""

    pk: tuple[str, ...]
    disposition: str
    columns: tuple[str, ...]
    rows: int


def loaded_tables(pipeline: dlt.Pipeline) -> dict[str, TableState]:
    """Read the loaded data tables back from DuckDB, with the pk/disposition dlt recorded for each."""
    schema_tables = pipeline.default_schema.tables
    result: dict[str, TableState] = {}
    with pipeline.sql_client() as client:
        for name, table in schema_tables.items():
            if name.startswith("_dlt") or not table.get("columns"):
                continue
            if not any(c.get("data_type") for c in table["columns"].values()):
                continue
            try:
                db_cols = [
                    r[0]
                    for r in client.execute_sql(
                        "select column_name from information_schema.columns "
                        "where table_schema = %s and table_name = %s",
                        client.dataset_name,
                        name,
                    )
                ]
                if not db_cols:
                    continue
                rows = client.execute_sql(
                    f"select count(*) from {client.make_qualified_table_name(name)}"
                )[0][0]
            except Exception:
                # declared in the schema but never created in DuckDB (nothing was loaded for it)
                continue
            parent = table.get("parent")
            pk = tuple(c for c, spec in table["columns"].items() if spec.get("primary_key"))
            disposition = str(table.get("write_disposition") or "append") if not parent else "child"
            result[name] = TableState(pk, disposition, tuple(db_cols), int(rows))
    return result


def null_key_rows(pipeline: dlt.Pipeline, table: str, pk: tuple[str, ...]) -> int:
    """Number of loaded rows whose primary key has a NULL part."""
    cond = " or ".join(f'"{c}" is null' for c in pk)
    with pipeline.sql_client() as client:
        return int(
            client.execute_sql(
                f"select count(*) from {client.make_qualified_table_name(table)} where {cond}"
            )[0][0]
        )


def fetch_rows(
    pipeline: dlt.Pipeline, table: str, columns: tuple[str, ...]
) -> list[tuple[Any, ...]]:
    cols = ", ".join(f'"{c}"' for c in columns)
    with pipeline.sql_client() as client:
        return [
            tuple(r)
            for r in client.execute_sql(
                f"select {cols} from {client.make_qualified_table_name(table)} order by 1"
            )
        ]


@dataclass(frozen=True)
class KeyCase:
    """One connector scenario for the key contract.

    `open(rev)` returns a context manager yielding a fresh dlt source built on top of stubbed transport.
    rev 0 is the baseline records; rev 1 is the same records with `probe_column` of one row changed.
    `pk` maps each table that must load to the normalised primary-key columns it must declare.
    """

    id: str
    open: Callable[[int], AbstractContextManager[Any]]
    pk: dict[str, tuple[str, ...]]
    probe: tuple[str, str, str, Any] | None = (
        None  # table, key column, value column, value at rev 1
    )
    min_rows: dict[str, int] = field(default_factory=dict)


def run_key_contract(case: KeyCase, root: Path) -> dict[str, TableState]:
    """Load rev 0 twice and rev 1 once through real dlt; assert (a) keys exist, (b) no duplicates, (c) update.

    Returns the tables after the baseline load, so a caller can assert connector-specific details.
    """
    pipeline = make_pipeline(root)
    with case.open(0) as source:
        pipeline.run(source)
    first = loaded_tables(pipeline)

    for table, expected_pk in case.pk.items():
        assert table in first, f"{case.id}: table {table!r} was not loaded; got {sorted(first)}"
        state = first[table]
        assert state.pk == expected_pk, (
            f"{case.id}: {table} declares pk {state.pk}, expected {expected_pk}"
        )
        missing = [c for c in state.pk if c not in state.columns]
        assert not missing, f"{case.id}: {table} pk columns {missing} missing from {state.columns}"
        assert state.rows >= case.min_rows.get(table, 1), (
            f"{case.id}: {table} has {state.rows} rows"
        )
        assert null_key_rows(pipeline, table, state.pk) == 0, (
            f"{case.id}: {table} has NULL key values"
        )

    with case.open(0) as source:
        pipeline.run(source)
    second = loaded_tables(pipeline)
    assert {t: s.rows for t, s in second.items()} == {t: s.rows for t, s in first.items()}, (
        f"{case.id}: re-running identical records changed row counts"
    )

    if case.probe is not None:
        table, key_col, value_col, new_value = case.probe
        if first[table].disposition != "append":
            with case.open(1) as source:
                pipeline.run(source)
            third = loaded_tables(pipeline)
            assert third[table].rows == first[table].rows, (
                f"{case.id}: a changed row was not updated in place"
            )
            assert new_value in [r[1] for r in fetch_rows(pipeline, table, (key_col, value_col))], (
                f"{case.id}: changed value {new_value!r} not found in {table}.{value_col}"
            )
    return first
