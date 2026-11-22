"""tests/unit/connector_key/test_connector_retry_replay.py

The runner re-extracts the same source object when an extract attempt fails. A connector that hands dlt a one-shot
generator object fails every retry with SourceExhausted and hides the real error; each registry connector that did
so is exercised here: first attempt raises, second succeeds, and when all attempts fail the real error surfaces.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.ingestion.dlt_sources.github import github_reactions
from dango.ingestion.dlt_sources.notion import notion_databases
from dango.ingestion.dlt_sources.strapi import strapi_source
from dango.ingestion.dlt_sources.zendesk import zendesk_support
from dango.ingestion.dlt_sources.zendesk.helpers.credentials import ZendeskCredentialsToken
from tests.factories.connector_contract import FakeHttp, loaded_tables, make_pipeline


class Flaky:
    """An HTTP handler that raises on its first `fail_first` calls, then serves `reply`."""

    def __init__(self, reply: Any, fail_first: int) -> None:
        self.reply, self.fail_first, self.calls = reply, fail_first, 0

    def __call__(self, request: Any) -> Any:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("boom: upstream went away")
        return self.reply(request) if callable(self.reply) else self.reply


def _graphql(request: Any) -> dict[str, Any]:
    variables = json.loads(request.body)["variables"]
    nodes = (
        []
        if "page_after" in variables
        else [
            {
                "id": "I_1",
                "number": 1,
                "title": "a",
                "reactions": {"totalCount": 0, "nodes": []},
                "comments": {"totalCount": 0, "nodes": []},
            }
        ]
    )
    connection = {"nodes": nodes, "pageInfo": {"endCursor": "c1"}}
    return {
        "data": {
            "repository": {variables["node_type"]: connection},
            "rateLimit": {"cost": 1, "remaining": 4999},
        }
    }


@dataclass(frozen=True)
class Case:
    id: str
    route: str
    reply: Any
    build: Callable[[], Any]
    table: str
    rows: int


CASES = [
    Case(
        "strapi",
        r"cms\.invalid/api/posts",
        {
            "meta": {"pagination": {"total": 2}},
            "data": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}],
        },
        lambda: strapi_source(endpoints=["posts"], api_secret_key="k", domain="cms.invalid"),
        "posts",
        2,
    ),
    Case(
        "notion",
        r"api\.notion\.com/v1/databases/db1/query",
        {
            "results": [{"id": "p1", "object": "page"}, {"id": "p2", "object": "page"}],
            "has_more": False,
        },
        lambda: notion_databases(database_ids=[{"id": "db1", "use_name": "Tasks"}], api_key="k"),
        "tasks",
        2,
    ),
    Case(
        "github_reactions",
        r"api\.github\.com/graphql",
        _graphql,
        lambda: github_reactions("o", "n", access_token="t", items_per_page=10),
        "issues",
        1,
    ),
    Case(
        "zendesk_support",
        r"acme\.zendesk\.com/api/v2/users\.json",
        {
            "users": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
            "meta": {"has_more": False},
            "links": {},
        },
        lambda: zendesk_support(
            credentials=ZendeskCredentialsToken(subdomain="acme", email="a@b.invalid", token="t"),
            load_all=False,
        ).with_resources("users"),
        "users",
        2,
    ),
]


@pytest.fixture
def runner(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with patch("dango.ingestion.dlt_runner.time.sleep"):
        yield DltPipelineRunner(root)


@pytest.mark.unit
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
class TestExtractRetryReplaysTheSource:
    def test_second_attempt_loads_after_the_first_fails(self, case, runner, tmp_path):
        flaky = Flaky(case.reply, fail_first=1)
        pipeline = make_pipeline(tmp_path)
        with FakeHttp().route(case.route, flaky).active():
            runner._run_extract_with_retry(pipeline, case.build(), max_retries=2)
        pipeline.normalize()
        pipeline.load()
        assert flaky.calls > 1
        assert loaded_tables(pipeline)[case.table].rows == case.rows

    def test_when_every_attempt_fails_the_real_error_surfaces(self, case, runner, tmp_path):
        flaky = Flaky(case.reply, fail_first=99)
        pipeline = make_pipeline(tmp_path)
        with FakeHttp().route(case.route, flaky).active():
            with pytest.raises(Exception, match="boom: upstream went away") as caught:
                runner._run_extract_with_retry(pipeline, case.build(), max_retries=2)
        assert "SourceExhausted" not in str(caught.value)
        assert flaky.calls >= 2  # the second attempt really called the API again
