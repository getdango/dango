"""tests/unit/test_connector_key_recovery.py

Failed-run recovery through `DltPipelineRunner` for a replace connector (Airtable) and a merge connector
(Freshdesk): after a failed sync a retry loads the data once, and `--full-refresh` still works.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest

from dango.config.models import DataSource, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.airtable_fake import airtable_record, airtable_stub, airtable_table
from tests.factories.connector_contract import FakeHttp


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_fake")
    monkeypatch.setenv("FRESHDESK_API_KEY", "key_fake")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    with (
        patch("dango.ingestion.dlt_runner.time.sleep"),
        patch("dango.ingestion.dlt_runner._apply_dlt_telemetry_env"),
    ):
        yield root


def _rows(root: Path, schema: str, table: str) -> list[tuple]:
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        return con.execute(f'select * from "{schema}"."{table}" order by 1').fetchall()
    finally:
        con.close()


def _airtable_source() -> DataSource:
    return DataSource(
        name="airtable_team",
        type=SourceType.AIRTABLE,
        generic_config={"base_id": "appX", "access_token_env": "AIRTABLE_ACCESS_TOKEN"},
    )


def _team(*names: str):
    tables = [airtable_table("Team Members", "Name")]
    return tables, {
        "tblAAAAAAAAAAAAAA": [
            airtable_record(f"rec{i}", Name=n, Role="dev") for i, n in enumerate(names)
        ]
    }


@pytest.mark.unit
class TestAirtableRunnerRecovery:
    def test_failed_extract_then_retry_then_full_refresh(self, project):
        source = _airtable_source()
        with airtable_stub(*_team("Ann", "Bob")):
            assert DltPipelineRunner(project).run_source(source)["status"] == "success"
        assert len(_rows(project, "raw_airtable_team", "team_members")) == 2

        tables, _ = _team()
        with airtable_stub(tables, {"tblAAAAAAAAAAAAAA": RuntimeError("api down")}):
            failed = DltPipelineRunner(project).run_source(source)
        assert failed["status"] == "failed"
        assert "api down" in failed["error"]  # the real cause, not a retry artefact
        assert (
            len(_rows(project, "raw_airtable_team", "team_members")) == 2
        )  # untouched by the failure

        with airtable_stub(*_team("Ann", "Bob", "Cy")):
            assert DltPipelineRunner(project).run_source(source)["status"] == "success"
        assert len(_rows(project, "raw_airtable_team", "team_members")) == 3

        with airtable_stub(*_team("Ann", "Bob", "Cy", "Di")):
            result = DltPipelineRunner(project).run_source(source, full_refresh=True)
        assert result["status"] == "success"
        assert len(_rows(project, "raw_airtable_team", "team_members")) == 4


def _freshdesk_source() -> DataSource:
    return DataSource(
        name="fd",
        type=SourceType.FRESHDESK,
        generic_config={
            "domain": "acme",
            "api_secret_key_env": "FRESHDESK_API_KEY",
            "endpoints": ["tickets"],
        },
    )


@contextmanager
def _freshdesk_api(tickets: list[dict] | Exception) -> Iterator[None]:
    def handler(request):
        if isinstance(tickets, Exception):
            raise tickets
        return tickets if parse_qs(urlparse(request.url).query)["page"] == ["1"] else []

    with FakeHttp().route(r"acme\.freshdesk\.com/api/v2/tickets\?", handler).active():
        yield


def _ticket(ticket_id, status=2, stamp="2024-05-01T10:00:00Z"):
    return {"id": ticket_id, "subject": f"s{ticket_id}", "status": status, "updated_at": stamp}


@pytest.mark.unit
class TestFreshdeskRunnerRecovery:
    def test_failed_extract_then_retry_then_full_refresh(self, project):
        source = _freshdesk_source()
        with _freshdesk_api([_ticket(1), _ticket(2)]):
            assert DltPipelineRunner(project).run_source(source)["status"] == "success"
        assert len(_rows(project, "raw_fd", "tickets")) == 2

        with _freshdesk_api(RuntimeError("api down")):
            failed = DltPipelineRunner(project).run_source(source)
        assert failed["status"] == "failed"
        assert "api down" in failed["error"]  # the real cause, not a retry artefact
        assert len(_rows(project, "raw_fd", "tickets")) == 2

        with _freshdesk_api(
            [_ticket(1, status=3, stamp="2024-05-02T10:00:00Z"), _ticket(2), _ticket(3)]
        ):
            assert DltPipelineRunner(project).run_source(source)["status"] == "success"
        assert len(_rows(project, "raw_fd", "tickets")) == 3

        with _freshdesk_api([_ticket(1), _ticket(2), _ticket(3), _ticket(4)]):
            result = DltPipelineRunner(project).run_source(source, full_refresh=True)
        assert result["status"] == "success"
        assert len(_rows(project, "raw_fd", "tickets")) == 4

    def test_failed_normalise_then_retry(self, project):
        # Rows without the declared key fail normalisation; the next run with good rows must still load.
        source = _freshdesk_source()
        bad = [{"subject": "no id", "updated_at": "2024-05-01T10:00:00Z"}]
        with _freshdesk_api(bad):
            failed = DltPipelineRunner(project).run_source(source)
        assert failed["status"] == "failed"
        with _freshdesk_api([_ticket(1), _ticket(2)]):
            assert DltPipelineRunner(project).run_source(source)["status"] == "success"
        assert len(_rows(project, "raw_fd", "tickets")) == 2
