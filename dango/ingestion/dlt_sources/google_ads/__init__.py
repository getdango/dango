"""Google Ads dlt source — query-driven performance metrics."""

from typing import Iterator, Union
from datetime import date, timedelta

import dlt
import json
import tempfile
from dlt.common.exceptions import MissingDependencyException
from dlt.common.typing import TDataItem
from dlt.sources import DltResource
from dlt.sources.credentials import GcpOAuthCredentials, GcpServiceAccountCredentials

from .helpers.data_processing import to_dict, flatten_row
from .settings import DEFAULT_LOOKBACK_DAYS, DEFAULT_QUERIES

try:
    from google.ads.googleads.client import GoogleAdsClient  # type: ignore
except ImportError:
    raise MissingDependencyException("Requests-OAuthlib", ["google-ads"])


def get_client(
    credentials: Union[GcpOAuthCredentials, GcpServiceAccountCredentials],
    dev_token: str,
    impersonated_email: str | None = None,
) -> GoogleAdsClient:
    """Creates a GoogleAdsClient from OAuth or service account credentials."""
    if isinstance(credentials, GcpOAuthCredentials):
        credentials.auth("https://www.googleapis.com/auth/adwords")
        conf = {
            "developer_token": dev_token,
            "use_proto_plus": True,
            **json.loads(credentials.to_native_representation()),
        }
        return GoogleAdsClient.load_from_dict(config_dict=conf)
    else:
        with tempfile.NamedTemporaryFile() as f:
            f.write(credentials.to_native_representation().encode())
            f.seek(0)
            return GoogleAdsClient.load_from_dict(
                config_dict={
                    "json_key_file_path": f.name,
                    "impersonated_email": impersonated_email,
                    "use_proto_plus": True,
                    "developer_token": dev_token,
                }
            )


def _compute_date_range(
    start_date: str | None, end_date: str | None
) -> tuple[str, str]:
    """Returns (effective_start, effective_end) as YYYY-MM-DD strings.

    Defaults: start = DEFAULT_LOOKBACK_DAYS ago, end = yesterday.
    """
    if end_date:
        effective_end = end_date
    else:
        effective_end = (date.today() - timedelta(days=1)).isoformat()

    if start_date:
        effective_start = start_date
    else:
        effective_start = (
            date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        ).isoformat()

    return effective_start, effective_end


def _execute_query(
    ga_service: object,
    customer_id: str,
    query: str,
) -> Iterator[TDataItem]:
    """Runs a GAQL query via search_stream and yields flattened rows."""
    stream = ga_service.search_stream(customer_id=customer_id, query=query)  # type: ignore[union-attr]
    for batch in stream:
        for row in batch.results:
            row_dict = to_dict(row)
            yield flatten_row(row_dict)


@dlt.source(name="google_ads", max_table_nesting=2)
def google_ads(
    credentials: Union[
        GcpOAuthCredentials, GcpServiceAccountCredentials
    ] = dlt.secrets.value,
    customer_id: str = dlt.secrets.value,
    dev_token: str = dlt.secrets.value,
    queries: list[dict[str, str]] = dlt.config.value,
    start_date: str | None = None,
    end_date: str | None = None,
    impersonated_email: str | None = None,
) -> list[DltResource]:
    """Loads Google Ads performance data via configurable GAQL queries.

    Each query in the queries list becomes a separate table. Queries use
    {start_date}/{end_date} placeholders replaced at runtime.

    Default queries load 5 tables: campaign_stats, ad_group_stats,
    keyword_stats, ad_stats, search_term_stats.
    """
    client = get_client(
        credentials=credentials,
        dev_token=dev_token,
        impersonated_email=impersonated_email,
    )
    ga_service = client.get_service("GoogleAdsService")

    effective_start, effective_end = _compute_date_range(start_date, end_date)

    # Fall back to defaults if queries list is empty
    active_queries = queries if queries else DEFAULT_QUERIES

    resources = []
    for q in active_queries:
        resource_name = q["resource_name"]
        formatted_query = q["query"].format(
            start_date=effective_start,
            end_date=effective_end,
        )
        resource = dlt.resource(
            _execute_query,
            name=resource_name,
            write_disposition="replace",
        )(
            ga_service=ga_service,
            customer_id=customer_id,
            query=formatted_query,
        )
        resources.append(resource)

    return resources
