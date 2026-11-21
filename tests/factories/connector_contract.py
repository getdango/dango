"""tests/factories/connector_contract.py

Factory for the vendored-connector primary-key contract tests: a fake HTTP transport (no sockets) and a
runner that loads synthetic records through real dlt into a temporary DuckDB, then checks the key contract.
"""

from __future__ import annotations

import json
import os
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

# dlt's anonymous usage telemetry would open a connection; these tests must not touch the network.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

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


Probe = tuple[
    str, str, str, Any
]  # table, key column, value column, value the changed row gets at rev 1
AppendProbe = tuple[str, str, Any]  # table, column, value of the one row that appears at rev 1


@dataclass(frozen=True)
class KeyCase:
    """One connector scenario for the key contract.

    `open(rev)` returns a context manager yielding a fresh dlt source built on top of stubbed transport.
    rev 0 is the baseline records. rev 1 is the same records after a change in the source system:
    for the tables in `probes` a row has a new value in its value column (same key), and for the tables in
    `append_probes` one new row appears.
    `pk` maps each table that must load to the normalised primary-key columns it must declare, and
    `disposition` pins the write disposition of each of those tables (a merge table silently becoming
    append would otherwise only show up as duplicates in a user's warehouse).

    What each check proves, per table kind:
    - every table: the key columns exist and are never NULL, and loading identical records again adds no rows
      (merge: the key dedups; append: the incremental cursor plus key dedup; replace: the table is rebuilt);
    - merge table with a probe: the changed row is updated in place (row count unchanged, new value present,
      old value gone), which only works when the declared key identifies the row;
    - replace table with a probe: the table reflects the new source state (the key plays no part);
    - append table with an append probe: exactly one row is added and nothing already loaded is added again.
      An append table never updates a row in place, so no update is asserted for it.
    """

    id: str
    open: Callable[[int], AbstractContextManager[Any]]
    pk: dict[str, tuple[str, ...]]
    disposition: dict[str, str]
    probes: tuple[Probe, ...] = ()
    append_probes: tuple[AppendProbe, ...] = ()
    min_rows: dict[str, int] = field(default_factory=dict)


def _values(pipeline: dlt.Pipeline, table: str, column: str) -> list[Any]:
    return [row[0] for row in fetch_rows(pipeline, table, (column,))]


def assert_key_contract(case: KeyCase, root: Path) -> dict[str, TableState]:
    """Load rev 0 twice and rev 1 once through real dlt and assert the contract described on `KeyCase`.

    Returns the tables after the baseline load, so a caller can assert connector-specific details.
    """
    missing_disposition = sorted(set(case.pk) - set(case.disposition))
    assert not missing_disposition, (
        f"{case.id}: no pinned write disposition for {missing_disposition}"
    )
    pipeline = make_pipeline(root)
    with case.open(0) as source:
        pipeline.run(source)
    first = loaded_tables(pipeline)

    for table, expected_pk in case.pk.items():
        assert table in first, f"{case.id}: table {table!r} was not loaded; got {sorted(first)}"
        state = first[table]
        assert state.disposition == case.disposition[table], (
            f"{case.id}: {table} has write disposition {state.disposition!r}, "
            f"expected {case.disposition[table]!r}"
        )
        assert sorted(state.pk) == sorted(expected_pk), (
            f"{case.id}: {table} declares pk {state.pk}, expected {expected_pk}"
        )
        missing = [c for c in state.pk if c not in state.columns]
        assert not missing, f"{case.id}: {table} pk columns {missing} missing from {state.columns}"
        assert state.rows >= case.min_rows.get(table, 1), (
            f"{case.id}: {table} has {state.rows} rows"
        )
        # defensive: dlt already rejects a NULL in a non-nullable key column at load
        assert null_key_rows(pipeline, table, state.pk) == 0, (
            f"{case.id}: {table} has NULL key values"
        )

    with case.open(0) as source:
        pipeline.run(source)
    second = loaded_tables(pipeline)
    assert {t: s.rows for t, s in second.items()} == {t: s.rows for t, s in first.items()}, (
        f"{case.id}: re-running identical records changed row counts"
    )

    for table, _key, column, new_value in case.probes:
        assert first[table].disposition != "append", (
            f"{case.id}: {table} is an append table; use append_probes"
        )
        assert new_value not in _values(pipeline, table, column), (
            f"{case.id}: {table}.{column} already holds {new_value!r} at rev 0, so the update "
            "check could not fail"
        )
    for table, column, new_value in case.append_probes:
        assert first[table].disposition == "append", (
            f"{case.id}: {table} is not an append table; use probes"
        )
        assert new_value not in _values(pipeline, table, column), (
            f"{case.id}: {table}.{column} already holds {new_value!r} at rev 0, so the new-row "
            "check could not fail"
        )

    if case.probes or case.append_probes:
        with case.open(1) as source:
            pipeline.run(source)
        third = loaded_tables(pipeline)
        for table, _key, column, new_value in case.probes:
            assert third[table].rows == first[table].rows, (
                f"{case.id}: {table} changed its row count on an update ({first[table].rows} -> "
                f"{third[table].rows}); the changed row was not updated in place"
            )
            assert new_value in _values(pipeline, table, column), (
                f"{case.id}: changed value {new_value!r} not found in {table}.{column}: "
                "the row was not updated in place"
            )
        for table, column, new_value in case.append_probes:
            assert third[table].rows == first[table].rows + 1, (
                f"{case.id}: {table} should gain exactly one new row, went "
                f"{first[table].rows} -> {third[table].rows}"
            )
            assert new_value in _values(pipeline, table, column), (
                f"{case.id}: the new row {new_value!r} is missing from {table}.{column}"
            )
    return first
