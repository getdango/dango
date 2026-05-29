"""Google analytics source helpers"""

from typing import Iterator, List
from pendulum.datetime import DateTime

import dlt
from dlt.common import logger, pendulum
from dlt.common.typing import TDataItem

from ..settings import START_DATE
from .data_processing import get_report
from google.analytics.data_v1beta.types import (
    Dimension,
    Metric,
)
from google.analytics.data_v1beta import BetaAnalyticsDataClient


def basic_report(
    client: BetaAnalyticsDataClient,
    rows_per_page: int,
    dimensions: List[str],
    metrics: List[str],
    property_id: int,
    resource_name: str,
    start_date: str,
    last_date: dlt.sources.incremental[DateTime],
) -> Iterator[TDataItem]:
    """
    Retrieves the data for a report given dimensions, metrics, and filters required for the report.

    Uses dlt incremental with lag for lookback — start_value is
    automatically adjusted by dlt to re-fetch recent data.

    Args:
        client: The Google Analytics client used to make requests.
        dimensions: Dimensions for the report.
        metrics: Metrics for the report.
        property_id: GA4 property ID.
        rows_per_page: Rows per page (default 1000, max 100000).
        resource_name: The resource name (for logging).
        start_date: Fallback start date for first sync.
        last_date: dlt incremental cursor (lag-adjusted by dlt).

    Returns:
        Generator of all rows of data in the report.
    """

    # Use lag-adjusted start from dlt if available, else configured start_date
    if last_date.start_value is not None:
        sv = last_date.start_value
        if isinstance(sv, DateTime):
            start_date = sv.to_date_string()
        elif isinstance(sv, str):
            start_date = sv
        else:
            start_date = str(sv)
    else:
        start_date = start_date or START_DATE

    # Calculate end_date as yesterday
    end_date = pendulum.yesterday().to_date_string()

    # Skip if start_date > end_date (already up to date, or timezone edge case)
    if start_date > end_date:
        logger.info(
            f"Skipping {resource_name}: already up to date (start_date={start_date} > end_date={end_date})"
        )
        return

    processed_response = get_report(
        client=client,
        property_id=property_id,
        # fill dimensions and metrics with the proper api client objects
        dimension_list=[Dimension(name=dimension) for dimension in dimensions],
        metric_list=[Metric(name=metric) for metric in metrics],
        limit=rows_per_page,
        start_date=start_date,
        end_date=end_date,
    )
    yield from processed_response
