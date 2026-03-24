# Zendesk

**Category:** Business & CRM | **Auth:** Basic | **Wizard:** Yes

## What Data You Get

- Support tickets, users, organizations, groups, and more
- **Incremental:** Yes — subsequent syncs fetch only updated records
- Tested with real data: ~650 rows on initial sync

## Setup

1. Log in to Zendesk as admin
2. Go to **Admin > Channels > API**
3. Enable token access
4. Run `dango source add`, select **Zendesk**, and enter your subdomain
5. Add credentials to `.dlt/secrets.toml`:
   ```toml
   [sources.zendesk_support]
   email = "your@email.com"
   password = "your-api-token"
   ```

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `subdomain` | Yes | Zendesk subdomain (e.g., `mycompany` for mycompany.zendesk.com) |
| `start_date` | No | Start date (default: `90daysAgo`) |

## Known Limitations

- Credentials go in `.dlt/secrets.toml` (not `.env`) — the wizard provides guidance but you must add them manually.
- The `subdomain` field is Dango-specific (used for credential routing) — it's not passed to the dlt source function as a kwarg.

## Troubleshooting

| Error | Fix |
|-------|-----|
| Authentication failed | Verify email + API token in `.dlt/secrets.toml` under `[sources.zendesk_support]` |
| Missing subdomain | Re-run wizard or add `subdomain` field to `sources.yml` manually |
