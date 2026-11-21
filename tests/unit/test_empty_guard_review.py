"""tests/unit/test_empty_guard_review.py

Review follow-ups for the per-table empty-replace guard: tables written by several resources,
dict-form `replace` dispositions (including a real REST API source), and secret redaction in the
failure text of the runner paths next to the guard.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import dlt
import pytest
from dlt.sources.rest_api import rest_api_source

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import EMPTY_REPLACE_PROTECTION_ERROR_TYPE, DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, count, ids, resource, sync

SECRET = "sk_live_REVIEWSECRET42"


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def shared(order, rows, other=None):
    """Two resources writing table `t` plus a merge table `b`.

    `order` is a list of (name, disposition, selected); `rows` feeds the first (replace) one.
    """

    @dlt.source(name="shared")
    def _src():
        for name, disposition, selected in order:
            hints = {"primary_key": "id"} if disposition == "merge" else {}
            data = rows if disposition == "replace" else []
            yield resource(name, data, disposition, table_name="t", selected=selected, **hints)
        yield resource("b", other or [], "merge", primary_key="id")

    return _src()


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "order",
    [
        [("a", "replace", True), ("a2", "merge", False)],
        [("a2", "merge", False), ("a", "replace", True)],
        [("a", "replace", True), ("a2", "merge", True)],
        [("a2", "merge", True), ("a", "replace", True)],
    ],
    ids=["unselected-last", "unselected-first", "selected-merge-last", "selected-merge-first"],
)
class TestSharedTable:
    def test_replace_writer_keeps_the_table_protected(self, tmp_path, method, order):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "sh", shared(order, [{"id": 1}, {"id": 2}]))["status"] == (
            "success"
        )
        result = sync(runner, method, "sh", shared(order, [], [{"id": 5}]))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "t: 2 rows" in result["error"]
        assert ids(runner, "sh", "t") == [1, 2]


def _dict_replace(rows, fact=None):
    @dlt.source(name="dictrep")
    def _src():
        yield resource("dim", rows, {"disposition": "replace"})
        if fact is not None:
            yield resource("fact", fact, "merge", primary_key="id")

    return _src()


@pytest.mark.unit
@pytest.mark.parametrize("method", METHODS)
class TestDictFormReplace:
    def test_only_dict_replace_zero_rows_is_blocked(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "dr", _dict_replace([{"id": 1}, {"id": 2}]))["status"] == (
            "success"
        )
        result = sync(runner, method, "dr", _dict_replace([]))
        assert result["status"] == "failed"
        assert result["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert "dim: 2 rows" in result["error"]
        assert ids(runner, "dr", "dim") == [1, 2]

    def test_dict_replace_with_merge_blocks_only_the_replace_table(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        first = sync(runner, method, "dm", _dict_replace([{"id": 1}], [{"id": 1}, {"id": 2}]))
        assert first["status"] == "success"
        ok = sync(runner, method, "dm", _dict_replace([{"id": 3}], []))
        assert ok["status"] == "success", ok.get("error")
        blocked = sync(runner, method, "dm", _dict_replace([], [{"id": 9}]))
        assert blocked["status"] == "failed"
        assert ids(runner, "dm", "dim") == [3]
        assert ids(runner, "dm", "fact") == [1, 2]

    def test_full_refresh_takes_the_replace_path_not_staging(self, tmp_path, method):
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "df", _dict_replace([{"id": 1}, {"id": 2}]))["status"] == (
            "success"
        )
        with patch.object(
            runner, "_full_refresh_via_staging", wraps=runner._full_refresh_via_staging
        ) as staging:
            reloaded = sync(runner, method, "df", _dict_replace([{"id": 7}]), full_refresh=True)
            emptied = sync(runner, method, "df", _dict_replace([]), full_refresh=True)
        staging.assert_not_called()
        assert reloaded["status"] == "success", reloaded.get("error")
        assert emptied["status"] == "failed"
        assert emptied["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
        assert ids(runner, "df", "dim") == [7]


@pytest.mark.unit
def test_detect_write_disposition_reads_the_dict_form(tmp_path):
    runner = DltPipelineRunner(tmp_path)
    assert runner._detect_write_disposition(_dict_replace([{"id": 1}])) is True

    @dlt.source(name="m")
    def _m():
        yield resource("x", [], {"disposition": "merge", "strategy": "scd2"})

    assert runner._detect_write_disposition(_m()) is False


@pytest.fixture
def rest_server():
    data: dict[str, list[dict[str, int]]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(data.get(self.path.strip("/").split("?")[0], [])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield data, f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.unit
@pytest.mark.parametrize("dim_disposition", ["replace", {"disposition": "replace"}], ids=str)
def test_rest_api_source_zero_rows_second_run_is_blocked(tmp_path, rest_server, dim_disposition):
    data, base_url = rest_server

    def source():
        return rest_api_source(
            {
                "client": {"base_url": base_url, "paginator": "single_page"},
                "resources": [
                    {
                        "name": "dim",
                        "write_disposition": dim_disposition,
                        "endpoint": {"path": "dim"},
                    },
                    {
                        "name": "fact",
                        "write_disposition": "merge",
                        "primary_key": "id",
                        "endpoint": {"path": "fact"},
                    },
                ],
            }
        )

    runner = DltPipelineRunner(tmp_path)
    data.update(dim=[{"id": 1}, {"id": 2}], fact=[{"id": 1}])
    assert sync(runner, "dlt", "ra", source())["status"] == "success"
    data.update(dim=[{"id": 3}], fact=[])
    merge_empty = sync(runner, "dlt", "ra", source())
    assert merge_empty["status"] == "success", merge_empty.get("error")
    assert ids(runner, "ra", "fact") == [1]
    data.update(dim=[], fact=[{"id": 2}])
    replace_empty = sync(runner, "dlt", "ra", source())
    assert replace_empty["status"] == "failed"
    assert replace_empty["error_type"] == EMPTY_REPLACE_PROTECTION_ERROR_TYPE
    assert ids(runner, "ra", "dim") == [3]
    assert count(runner, "ra", "fact") == 1


def _failing_merge(fail):
    @dlt.resource(name="m", write_disposition="merge", primary_key="id")
    def m():
        if fail:
            raise RuntimeError(f"upstream rejected {SECRET} for this request")
        yield {"id": 1}

    @dlt.source(name="lk")
    def s():
        return m

    return s()


@pytest.mark.unit
class TestSecretsNeverReturned:
    def test_staging_failure_text_is_redacted(self, tmp_path):
        (tmp_path / ".env").write_text(f"API_TOKEN={SECRET}\n", encoding="utf-8")
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, "native", "lk", _failing_merge(False))["status"] == "success"
        result = sync(runner, "native", "lk", _failing_merge(True), full_refresh=True)
        assert result["status"] == "failed"
        assert "never touched" in result["error"]
        assert SECRET not in result["error"]

    def test_run_source_failure_text_is_redacted(self, tmp_path):
        (tmp_path / ".env").write_text(f"API_TOKEN={SECRET}\n", encoding="utf-8")
        runner = DltPipelineRunner(tmp_path)
        config = DataSource(
            name="lk",
            type=SourceType.DLT_NATIVE,
            dlt_native=DltNativeConfig(source_module="m", source_function="f", function_kwargs={}),
        )
        boom = RuntimeError(f"upstream rejected {SECRET} for this request")
        with patch.object(runner, "_run_dlt_native_source", side_effect=boom):
            result = runner.run_source(config)
        assert result["status"] == "failed"
        assert "upstream rejected" in result["error"]
        assert SECRET not in result["error"]
