"""Google Ads dlt source — query-driven performance metrics."""

import re
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
from .settings import DEFAULT_QUERIES

try:
    from google.ads.googleads.client import GoogleAdsClient  # type: ignore
except ImportError:
    raise MissingDependencyException("Google Ads", ["google-ads"])


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


def _extract_pk_columns(query: str) -> list[str]:
    """Derive primary key columns from a GAQL SELECT clause.

    Non-metric columns form the composite PK.  After flatten_row():
    - segments.date -> date  (segments promoted to top level)
    - campaign.id -> campaign_id  (entity fields prefix-joined)
    - metrics.* are excluded (they're measures, not keys)
    """
    select_match = re.search(r"SELECT\s+(.*?)\s+FROM", query, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return []

    columns = [col.strip().rstrip(",") for col in select_match.group(1).split(",")]
    pk_columns: list[str] = []

    for col in columns:
        col = col.strip()
        if col.startswith("metrics."):
            continue  # Metrics are measures, not keys
        if col.startswith("segments."):
            # segments.date -> date (promoted to top level by flatten_row)
            pk_columns.append(col.split(".", 1)[1])
        else:
            # campaign.id -> campaign_id, ad_group_criterion.keyword.text -> ad_group_criterion_keyword_text
            pk_columns.append(col.replace(".", "_"))

    return pk_columns


def _execute_query_incremental(
    ga_service: object,
    customer_id: str,
    query_template: str,
    end_date: str,
    start_date: str = "2020-01-01",
    date_cursor: dlt.sources.incremental[str] = dlt.sources.incremental("date"),
    resource_name: str = "",
) -> Iterator[TDataItem]:
    """Runs a GAQL query with incremental date tracking and yields flattened rows.

    On subsequent syncs, date_cursor.start_value is lag-adjusted by dlt,
    so the query automatically re-fetches the lookback window.
    """
    # Use lag-adjusted cursor start if available, else configured start_date
    effective_start = start_date
    if date_cursor.start_value is not None:
        effective_start = date_cursor.start_value

    query = query_template.format(start_date=effective_start, end_date=end_date)

    try:
        stream = ga_service.search_stream(customer_id=customer_id, query=query)  # type: ignore[union-attr]
        for batch in stream:
            for row in batch.results:
                row_dict = to_dict(row)
                yield flatten_row(row_dict)
    except Exception as e:
        raise RuntimeError(
            f"Google Ads query '{resource_name}' failed: {e}\n"
            f"  GAQL: {query[:200]}{'...' if len(query) > 200 else ''}\n"
            f"  Validate at: https://developers.google.com/google-ads/api/fields/v17/overview"
        ) from e


@dlt.source(name="google_ads", max_table_nesting=2)
def google_ads(
    credentials: Union[GcpOAuthCredentials, GcpServiceAccountCredentials] = dlt.secrets.value,
    customer_id: str = dlt.secrets.value,
    dev_token: str = dlt.secrets.value,
    queries: list[dict[str, str]] = dlt.config.value,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int = 90,
    impersonated_email: str | None = None,
) -> list[DltResource]:
    """Loads Google Ads performance data via configurable GAQL queries.

    Each query becomes a separate table with merge write disposition.
    Uses dlt incremental with lag for lookback — re-fetches recent data
    to capture attribution changes while preserving full history.

    First sync loads from start_date. Subsequent syncs load from
    (last_date - lookback_days) to yesterday.
    """
    client = get_client(
        credentials=credentials,
        dev_token=dev_token,
        impersonated_email=impersonated_email,
    )
    ga_service = client.get_service("GoogleAdsService")

    effective_end = end_date or (date.today() - timedelta(days=1)).isoformat()
    effective_start = start_date or (date.today() - timedelta(days=90)).isoformat()

    # Fall back to defaults if queries list is empty
    active_queries = queries if queries else DEFAULT_QUERIES

    resources = []
    for q in active_queries:
        resource_name = q["resource_name"]
        pk_columns = _extract_pk_columns(q["query"])

        resource = dlt.resource(
            _execute_query_incremental,
            name=resource_name,
            write_disposition="merge",
            primary_key=pk_columns,
        )(
            ga_service=ga_service,
            customer_id=customer_id,
            query_template=q["query"],
            end_date=effective_end,
            start_date=effective_start,
            date_cursor=dlt.sources.incremental(
                "date",  # flatten_row promotes segments.date to "date"
                lag=lookback_days,  # days (string date cursor)
            ),
            resource_name=resource_name,
        )
        resources.append(resource)

    return resources
