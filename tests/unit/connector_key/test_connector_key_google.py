"""tests/unit/connector_key/test_connector_key_google.py

Primary-key contract for the vendored Google Analytics and Google Ads connectors: the Google SDKs are replaced
by small stand-ins (Analytics: report types and client; Ads: GAQL-shaped rows) and the rows load through real dlt.
"""

from __future__ import annotations

import enum
import importlib
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pendulum
import pytest
from dlt.common.configuration.specs.gcp_credentials import GcpDefaultCredentials
from dlt.sources.credentials import GcpServiceAccountCredentials

from tests.factories.connector_contract import KeyCase, assert_key_contract
from tests.factories.sdk_fakes import fake_sdk

GA_PKG = "dango.ingestion.dlt_sources.google_analytics"
ADS_PKG = "dango.ingestion.dlt_sources.google_ads"


class _Named:
    def __init__(self, name: str = "", **_kw):
        self.name = name


class _Value:
    def __init__(self, value: str = ""):
        self.value = value


class _MetricType(enum.Enum):
    TYPE_INTEGER = 1


class _Header:
    def __init__(self, name: str, type_=None):
        self.name, self.type_ = name, type_


class _Row:
    def __init__(self, dimension_values, metric_values):
        self.dimension_values, self.metric_values = dimension_values, metric_values


class _Request:
    def __init__(self, dimensions=(), metrics=(), **_kw):
        self.dimensions, self.metrics = dimensions, metrics


class _Response:
    def __init__(self, dimension_headers, metric_headers, rows, row_count):
        self.dimension_headers, self.metric_headers = dimension_headers, metric_headers
        self.rows, self.row_count = rows, row_count


def _ga_sdk():
    return fake_sdk(
        {
            "google.analytics.data_v1beta": {"BetaAnalyticsDataClient": object},
            "google.analytics.data_v1beta.types": {
                "Dimension": _Named,
                "Metric": _Named,
                "DateRange": _Named,
                "MetricType": _MetricType,
                "RunReportRequest": _Request,
                "RunReportResponse": _Response,
            },
            "apiclient.discovery": {},
            "proto": {},
        },
        GA_PKG,
    )


def _ads_sdk():
    return fake_sdk({"google.ads.googleads.client": {}, "proto": {}}, ADS_PKG)


def _fake_credentials() -> GcpServiceAccountCredentials:
    # every field set; the union credentials spec of the sources still tries its OAuth member first, which
    # would probe for default credentials (see _no_default_credentials)
    return GcpServiceAccountCredentials(
        project_id="fake-project",
        private_key="not-a-key",
        private_key_id="fake-key-id",
        client_email="svc@fake-project.invalid",
    )


def _no_default_credentials():
    """dlt looks for Google default credentials (a metadata-server probe) while resolving the union spec."""
    return patch.object(
        GcpDefaultCredentials,
        "_get_default_credentials",
        staticmethod(lambda *_a, **_k: (None, None)),
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
                _Row(
                    [_Value(v) for v in values],
                    [_Value(str(self.sessions * n))],
                )
            )
        return _Response(
            [_Header(d) for d in dims],
            [_Header("sessions", _MetricType.TYPE_INTEGER)],
            rows,
            len(rows),
        )


def _ga_case(dimensions, table, pk, case_id):
    @contextmanager
    def open_(rev):
        fake = type("Fake", (_FakeGaClient,), {"sessions": 99 if rev else 10})
        with (
            _ga_sdk(),
            patch.object(GcpServiceAccountCredentials, "to_native_credentials", lambda self: None),
            _no_default_credentials(),
        ):
            module = importlib.import_module(GA_PKG)
            module.BetaAnalyticsDataClient = fake
            yield module.google_analytics(
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
        assert_key_contract(case, tmp_path)


with _ads_sdk():
    DEFAULT_QUERIES = importlib.import_module(ADS_PKG + ".settings").DEFAULT_QUERIES

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
        with _ads_sdk(), _no_default_credentials():
            ads_module = importlib.import_module(ADS_PKG)
            ads_module.get_client = lambda **_kw: client
            ads_module.to_dict = lambda row: row
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
        assert_key_contract(_ads_case(query), tmp_path)

    def test_user_query_without_a_primary_key_falls_back_to_date(self, tmp_path):
        query = {
            "resource_name": "daily_clicks",
            "query": "SELECT segments.date, metrics.clicks FROM customer WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'",
            "primary_key": ["date"],
        }
        assert_key_contract(_ads_case(query), tmp_path)
