import datetime
from typing import Any
from dlt.common.typing import TDataItem
import proto
import json


def to_dict(item: Any) -> TDataItem:
    """Converts a protobuf message to a Python dict."""
    return json.loads(
        proto.Message.to_json(
            item,
            preserving_proto_field_name=True,
            use_integers_for_enums=False,
            including_default_value_fields=False,
        )
    )


_INTEGER_METRICS = {
    "impressions", "clicks", "conversions", "video_views",
    "interactions", "gmail_forwards", "gmail_saves", "gmail_secondary_clicks",
}
_FLOAT_METRICS = {
    "ctr", "average_cpc", "average_cpm", "average_cpv",
    "conversions_value", "cost_per_conversion",
    "search_impression_share", "search_rank_lost_impression_share",
    "search_budget_lost_impression_share",
    "search_top_impression_percentage", "search_absolute_top_impression_percentage",
    "interaction_rate", "average_cost",
}


def flatten_row(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Flattens a Google Ads API response row into a flat dict.

    - segments.* promoted to top level (e.g. segments.date -> date)
    - metrics.* flattened, with cost_micros converted to cost (/ 1_000_000)
    - Entity fields prefix-joined: campaign.id -> campaign_id
    """
    result: dict[str, Any] = {}

    # Promote segments to top level
    if "segments" in row_dict:
        for key, value in row_dict["segments"].items():
            if key == "date" and isinstance(value, str):
                result[key] = datetime.date.fromisoformat(value)
            else:
                result[key] = value

    # Flatten metrics, converting cost_micros -> cost and casting known types
    if "metrics" in row_dict:
        for key, value in row_dict["metrics"].items():
            if key == "cost_micros":
                result["cost"] = int(value) / 1_000_000 if value else 0.0
            elif value is not None and key in _INTEGER_METRICS:
                result[key] = int(value)
            elif value is not None and key in _FLOAT_METRICS:
                result[key] = float(value)
            else:
                result[key] = value

    # Flatten entity fields (campaign, ad_group, etc.)
    for key, value in row_dict.items():
        if key in ("segments", "metrics"):
            continue
        if isinstance(value, dict):
            _flatten_entity(result, key, value)
        else:
            result[key] = value

    return result


def _flatten_entity(
    target: dict[str, Any], prefix: str, obj: dict[str, Any]
) -> None:
    """Recursively flattens nested entity dicts with underscore-joined keys.

    Example: {"campaign": {"id": 1, "name": "foo"}} -> campaign_id, campaign_name
    """
    for key, value in obj.items():
        flat_key = f"{prefix}_{key}"
        if isinstance(value, dict):
            _flatten_entity(target, flat_key, value)
        else:
            target[flat_key] = value
