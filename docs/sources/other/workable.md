# Workable

**Category:** Business & CRM | **Auth:** API Key | **Wizard:** Yes

## What Data You Get

- Jobs, candidates, activities, and recruitment pipeline data
- **Incremental:** Yes — subsequent syncs fetch only new data
- Tested with real data: 11 rows on initial sync

## Setup

1. Log in to Workable
2. Go to **Settings > Integrations > API**
3. Generate an access token
4. Run `dango source add`, select **Workable**, and enter the token and subdomain

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `access_token_env` | Yes | Workable API Access Token (env var: `WORKABLE_ACCESS_TOKEN`) |
| `subdomain` | Yes | Workable subdomain (e.g., `yourcompany`) |
| `start_date` | No | Start date (default: `2000-01-01`) |
| `load_details` | No | Load detailed data like activities (default: false) |

## Known Limitations

- The `subdomain` parameter is passed directly to the dlt source function (unlike Zendesk where it's Dango-only).
- `start_date` accepts string format (e.g., `"2024-01-01"`).

## Troubleshooting

| Error | Fix |
|-------|-----|
| start_date type error | Use string format `"YYYY-MM-DD"`, not a date object (fixed in Phase 5) |
