# Notion

**Category:** Files & Storage | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and create an integration
2. Copy the Internal Integration Token (starts with `secret_`)
3. Share your Notion databases with the integration (open database > Share > invite integration)
4. Run `dango source add`, select **Notion**, and enter the token

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `api_key_env` | Yes | Notion Integration Token (env var: `NOTION_API_KEY`) |
| `database_ids` | No | Database IDs to sync as JSON array (empty = all shared databases) |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- No incremental loading — full refresh on each sync
- Each database must be explicitly shared with the integration
