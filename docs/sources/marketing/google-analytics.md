# Google Analytics (GA4)

**Category:** Marketing & Analytics | **Auth:** OAuth | **Wizard:** Yes

## What Data You Get

Default query tables (configurable):
- **traffic** — Sessions, users, bounce rate by date
- **pages** — Page views, engagement by page path
- **landing_pages** — Landing page performance
- **geo** — Traffic by country/city

Custom queries can be added for any GA4 dimensions and metrics.

## Setup

1. Run `dango source add` and select **Google Analytics (GA4)**
2. The wizard triggers Google OAuth — browser opens for consent
3. Grant access to your Google Analytics data
4. Enter your GA4 Property ID (numeric, found in Admin > Property Settings)

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `property_id` | Yes | GA4 Property ID (e.g., `337090025`) |
| `start_date` | No | Start date — `YYYY-MM-DD` or relative like `90daysAgo` (default: `90daysAgo`) |

**Pip dependency:** `google-analytics-data` (installed automatically)

## Known Limitations

- **Duplicate row risk.** Vendored GA4 source uses append mode with no dedup. Re-syncing the same date range produces duplicates. Use full refresh if needed (see [Known Limitations](../known-limitations.md)).
- **Data sampling.** GA4 may return sampled data for large date ranges. Use shorter `start_date` windows for accuracy.
- **Token refresh is automatic.** Google OAuth tokens are refreshed by dlt on each sync — no manual intervention needed.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `start_date` type error | Use string format like `"90daysAgo"` or `"2024-01-01"`, not a date object |
| Property not found | Verify the Property ID in GA4 Admin > Property Settings |
| Insufficient permissions | The OAuth account needs Viewer access to the GA4 property |
