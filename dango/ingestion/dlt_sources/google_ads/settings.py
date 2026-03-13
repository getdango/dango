"""Default settings for Google Ads dlt source."""

DEFAULT_LOOKBACK_DAYS = 90

# Each query becomes a separate dlt resource/table.
# {start_date} and {end_date} are replaced at runtime (YYYY-MM-DD).
DEFAULT_QUERIES = [
    {
        "resource_name": "campaign_stats",
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "campaign.status, "
            "campaign.advertising_channel_type, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.conversions_value, "
            "metrics.ctr, "
            "metrics.average_cpc, "
            "metrics.average_cpm "
            "FROM campaign "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "ad_group_stats",
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "ad_group.status, "
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
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.ctr, "
            "metrics.average_cpc "
            "FROM keyword_view "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "ad_stats",
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "ad_group.id, "
            "ad_group_ad.ad.id, "
            "ad_group_ad.ad.name, "
            "ad_group_ad.ad.type, "
            "ad_group_ad.status, "
            "metrics.impressions, "
            "metrics.clicks, "
            "metrics.cost_micros, "
            "metrics.conversions, "
            "metrics.ctr "
            "FROM ad_group_ad "
            "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
        ),
    },
    {
        "resource_name": "search_term_stats",
        "query": (
            "SELECT "
            "segments.date, "
            "campaign.id, "
            "campaign.name, "
            "ad_group.id, "
            "ad_group.name, "
            "search_term_view.search_term, "
            "ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, "
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
]
