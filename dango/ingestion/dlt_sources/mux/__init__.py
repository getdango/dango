"""Loads Mux views data using https://docs.mux.com/api-reference"""

from typing import Iterable

import dlt
from dlt.common import pendulum
from dlt.sources.helpers import requests
from requests.auth import HTTPBasicAuth
from dlt.common.typing import TDataItem
from dlt.sources import DltResource

from .settings import API_BASE_URL, DEFAULT_LIMIT


@dlt.source
def mux_source(
    start_date: str | None = None,
    end_date: str | None = None,
) -> Iterable[DltResource]:
    """
    Source function that loads all video assets and video views for a configurable date range.

    Args:
        start_date: Start of the date range (ISO 8601 string or datetime). Defaults to 30 days ago.
        end_date: End of the date range (ISO 8601 string or datetime). Defaults to today.

    Yields:
        DltResource: Video assets and video views to be loaded.
    """
    yield assets_resource
    yield views_resource(start_date=start_date, end_date=end_date)


@dlt.resource(write_disposition="merge")
def assets_resource(
    mux_api_access_token: str = dlt.secrets.value,
    mux_api_secret_key: str = dlt.secrets.value,
    limit: int = DEFAULT_LIMIT,
) -> Iterable[TDataItem]:
    """
    Resource function that yields metadata about every asset to be loaded.

    Args:
        mux_api_access_token (str): API access token for Mux.
        mux_api_secret_key (str): API secret key for Mux.
        limit (int): Limit on the number of assets to retrieve. Defaults to DEFAULT_LIMIT.

    Yields:
        TDataItem: Data of each asset.
    """
    url = f"{API_BASE_URL}/video/v1/assets"
    params = {"limit": limit}

    response = requests.get(
        url, params=params, auth=HTTPBasicAuth(mux_api_access_token, mux_api_secret_key)
    )
    response.raise_for_status()
    yield response.json()["data"]


@dlt.resource(write_disposition="replace")
def views_resource(
    mux_api_access_token: str = dlt.secrets.value,
    mux_api_secret_key: str = dlt.secrets.value,
    limit: int = DEFAULT_LIMIT,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Iterable[TDataItem]:
    """
    Resource function that yields metadata about video views for a configurable date range.

    Args:
        mux_api_access_token: API access token for Mux.
        mux_api_secret_key: API secret key for Mux.
        limit: Limit on the number of video views to retrieve. Defaults to DEFAULT_LIMIT.
        start_date: Start of the date range (ISO 8601 string or datetime). Defaults to 30 days ago.
        end_date: End of the date range (ISO 8601 string or datetime). Defaults to today.

    Yields:
        TDataItem: Data for each video view in the date range.
    """
    url = f"{API_BASE_URL}/data/v1/video-views"
    page = 1

    if end_date:
        end_dt = pendulum.parse(str(end_date)) if not isinstance(end_date, pendulum.DateTime) else end_date
    else:
        end_dt = pendulum.today()

    if start_date:
        start_dt = pendulum.parse(str(start_date)) if not isinstance(start_date, pendulum.DateTime) else start_date
    else:
        start_dt = end_dt.subtract(days=30)

    timeframe_start = int(start_dt.timestamp())
    timeframe_end = int(end_dt.timestamp())

    while True:
        params = {"limit": limit, "page": page, "timeframe[]": [timeframe_start, timeframe_end]}
        response = requests.get(
            url,
            params=params,  # type: ignore
            auth=HTTPBasicAuth(mux_api_access_token, mux_api_secret_key),
        )
        response.raise_for_status()
        if response.json()["data"] == []:
            break
        yield response.json()["data"]
        page += 1
