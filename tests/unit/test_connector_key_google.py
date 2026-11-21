"""tests/unit/test_connector_key_google.py

Primary-key contract for the vendored Google Analytics and Google Ads connectors: the client object is replaced
(Analytics: real protobuf report types; Ads: GAQL-shaped rows) and the rows load through real dlt.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pendulum
import pytest
from dlt.sources.credentials import GcpServiceAccountCredentials
from google.analytics.data_v1beta.types import (
    DimensionHeader,
    DimensionValue,
    MetricHeader,
    MetricType,
    MetricValue,
    Row,
    RunReportResponse,
)

import dango.ingestion.dlt_sources.google_ads as ads_module
from dango.ingestion.dlt_sources.google_ads.settings import DEFAULT_QUERIES
from dango.ingestion.dlt_sources.google_analytics import google_analytics
from tests.factories.connector_contract import KeyCase, run_key_contract


def _fake_credentials() -> GcpServiceAccountCredentials:
    return GcpServiceAccountCredentials(
        project_id="fake-project", private_key="not-a-key", client_email="svc@fake-project.invalid"
    )


# -- Google Analytics ------------------------------------------------------------------------------------------

YESTERDAY = pendulum.yesterday("UTC")
DATE_FORMATS = {
    "date": "YYYYMMDD",
    "yearMonth": "YYYYMM",
    "dateHour": "YYYYMMDDHH",
    "year": "YYYY",
}


class _FakeGaClient:
    sessions = 10

    def __init__(self, *_a, **_kw):
        pass

    def run_report(self, request):
        dims = [d.name for d in request.dimensions]
        rows = []
        for n in (1, 2):
            values = [
                DATE_FORMATS[d] and YESTERDAY.format(DATE_FORMATS[d])
                if d in DATE_FORMATS
                else f"{d}-{n}"
                for d in dims
            ]
            rows.append(
                Row(
                    dimension_values=[DimensionValue(value=v) for v in values],
                    metric_values=[MetricValue(value=str(self.sessions * n))],
                )
            )
        return RunReportResponse(
            dimension_headers=[DimensionHeader(name=d) for d in dims],
            metric_headers=[MetricHeader(name="sessions", type_=MetricType.TYPE_INTEGER)],
            rows=rows,
            row_count=len(rows),
        )


def _ga_case(dimensions, table, pk, case_id):
    @contextmanager
    def open_(rev):
        fake = type("Fake", (_FakeGaClient,), {"sessions": 99 if rev else 10})
        base = "dango.ingestion.dlt_sources.google_analytics.BetaAnalyticsDataClient"
        with (
            patch(base, fake),
            patch.object(GcpServiceAccountCredentials, "to_native_credentials", lambda self: None),
        ):
            yield google_analytics(
                credentials=_fake_credentials(),
                property_id=123456,
                queries=[
                    {
                        "resource_name": table,
                        "dimensions": list(dimensions),
                        "metrics": ["sessions"],
                    }
                ],
                start_date=YESTERDAY.subtract(days=3).format("YYYY-MM-DD"),
            )

    first = dimensions[0]
    key_col = re.sub(r"(?<!^)(?=[A-Z])", "_", first).lower().replace(":", "_")
    return KeyCase(id=case_id, open=open_, pk={table: pk}, probe=(table, key_col, "sessions", 198))


@pytest.mark.unit
class TestGoogleAnalyticsKeyContract:
    @pytest.mark.parametrize(
        ("dimensions", "pk"),
        [
            (["sessionSource", "sessionMedium"], ("session_source", "session_medium", "date")),
            (["pagePath"], ("page_path", "date")),
            (["date", "country"], ("date", "country")),
            (["yearMonth", "deviceCategory"], ("year_month", "device_category")),
            (["dateHour", "browser"], ("date_hour", "browser")),
            (["customEvent:my_param"], ("custom_event_my_param", "date")),
        ],
    )
    def test_dimension_names_become_key_columns(self, dimensions, pk, tmp_path):
        case = _ga_case(dimensions, "my_report", pk, f"ga[{'+'.join(dimensions)}]")
        run_key_contract(case, tmp_path)


# -- Google Ads ------------------------------------------------------------------------------------------------


def _nested_row(query: str, n: int, clicks: int) -> dict:
    """A GAQL-shaped row (what `to_dict(row)` returns) for the fields in a query's SELECT list."""
    fields = [f.strip() for f in re.search(r"SELECT (.*?) FROM", query, re.S).group(1).split(",")]
    row: dict = {}
    for field in fields:
        node = row
        *path, leaf = field.split(".")
        for part in path:
            node = node.setdefault(part, {})
        if field == "segments.date":
            node[leaf] = YESTERDAY.format("YYYY-MM-DD")
        elif field == "metrics.clicks":
            node[leaf] = str(clicks)
        elif leaf.endswith("id"):
            node[leaf] = str(1000 + n)
        elif leaf.startswith(("cost", "impressions")) or field.startswith("metrics."):
            node[leaf] = n
        else:
            node[leaf] = f"{field}-{n}"
    return row


class _FakeAdsService:
    def __init__(self, clicks: int):
        self.clicks = clicks

    def search_stream(self, customer_id, query):
        rows = [_nested_row(query, n, self.clicks * n) for n in (1, 2)]
        yield SimpleNamespace(results=rows)


def _ads_case(query: dict):
    @contextmanager
    def open_(rev):
        service = _FakeAdsService(clicks=50 if rev else 5)
        client = SimpleNamespace(get_service=lambda _name: service)
        with (
            patch.object(ads_module, "get_client", lambda **_kw: client),
            patch.object(ads_module, "to_dict", lambda row: row),
        ):
            yield ads_module.google_ads(
                credentials=_fake_credentials(),
                customer_id="1234567890",
                dev_token="dev",
                queries=[query],
                start_date=YESTERDAY.subtract(days=3).format("YYYY-MM-DD"),
                end_date=YESTERDAY.format("YYYY-MM-DD"),
            )

    pk = tuple(query["primary_key"])
    return KeyCase(
        id=f"google_ads[{query['resource_name']}]",
        open=open_,
        pk={query["resource_name"]: pk},
        probe=(query["resource_name"], "campaign_id", "clicks", 100)
        if "clicks" in query["query"] and "campaign_id" in pk
        else None,
    )


@pytest.mark.unit
class TestGoogleAdsKeyContract:
    @pytest.mark.parametrize("query", DEFAULT_QUERIES, ids=lambda q: q["resource_name"])
    def test_every_default_query_key_exists_after_flatten_and_normalise(self, query, tmp_path):
        run_key_contract(_ads_case(query), tmp_path)

    def test_user_query_without_a_primary_key_falls_back_to_date(self, tmp_path):
        query = {
            "resource_name": "daily_clicks",
            "query": "SELECT segments.date, metrics.clicks FROM customer WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'",
            "primary_key": ["date"],
        }
        run_key_contract(_ads_case(query), tmp_path)
