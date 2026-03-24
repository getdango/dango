# Pipedrive

**Category:** Business & CRM | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Log in to Pipedrive
2. Go to **Settings > Personal > API**
3. Copy your API token
4. Run `dango source add`, select **Pipedrive**, and enter the token

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `pipedrive_api_key_env` | Yes | Pipedrive API Token (env var: `PIPEDRIVE_API_KEY`) |
| `since_timestamp` | No | Start date for incremental loading (default: `1970-01-01`) |
| `resources` | No | Resources to sync (16 available, see below) |

Available resources: activities, deals, leads, organizations, persons, pipelines, stages, products, notes, users, files, filters, goals, deal_fields, person_fields, organization_fields

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported via `since_timestamp`
