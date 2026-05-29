"""Default settings for Google Ads dlt source.

Each query becomes a separate dlt resource/table.
{start_date} and {end_date} are replaced at runtime (YYYY-MM-DD).
primary_key lists the immutable identifier columns (after flatten_row)
used for merge write disposition.
"""

DEFAULT_QUERIES = [
    {
        "resource_name": "campaign_stats",
        "primary_key": ["date", "campaign_id"],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "campaign.status, "
            "campaign.advertising_channel_type, "
            "campaign.bidding_strategy_type, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.conversions_value, "
            "metrics.ctr, "
            "metrics.average_cpc, "
            "metrics.average_cpm, "
            "metrics.search_impression_share, "
            "metrics.search_rank_lost_impression_share "
            "FROM campaign "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "ad_group_stats",
        "primary_key": ["date", "campaign_id", "ad_group_id"],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "ad_group.status, "
            "ad_group.type, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.conversions_value, "
            "metrics.ctr, "
            "metrics.average_cpc "
            "FROM ad_group "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "keyword_stats",
        "primary_key": [
            "date",
            "campaign_id",
            "ad_group_id",
            "ad_group_criterion_keyword_text",
            "ad_group_criterion_keyword_match_type",
        ],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, "
            "ad_group_criterion.quality_info.quality_score, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.ctr, "
            "metrics.average_cpc, "
            "metrics.search_impression_share "
            "FROM keyword_view "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "ad_stats",
        "primary_key": ["date", "campaign_id", "ad_group_id", "ad_group_ad_ad_id"],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "ad_group_ad.ad.id, "
            "ad_group_ad.ad.name, "
            "ad_group_ad.ad.type, "
            "ad_group_ad.status, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.conversions_value, "
            "metrics.ctr "
            "FROM ad_group_ad "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "search_term_stats",
        "primary_key": [
            "date",
            "campaign_id",
            "ad_group_id",
            "search_term_view_search_term",
        ],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "search_term_view.search_term, "
            "search_term_view.status, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.ctr, "
            "metrics.average_cpc "
            "FROM search_term_view "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "geographic_stats",
        "primary_key": [
            "date",
            "campaign_id",
            "geographic_view_country_criterion_id",
            "geographic_view_location_type",
        ],
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "geographic_view.country_criterion_id, "
            "geographic_view.location_type, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.conversions_value "
            "FROM geographic_view "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
]
