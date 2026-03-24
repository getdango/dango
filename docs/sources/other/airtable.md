# Airtable

**Category:** Marketing & Analytics | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens)
2. Create a personal access token with required scopes
3. Grant access to your bases
4. Copy the token
5. Run `dango source add`, select **Airtable**, and enter the Base ID and token

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `base_id` | Yes | Airtable Base ID (starts with `app`) |
| `access_token_env` | Yes | Personal Access Token (env var: `AIRTABLE_ACCESS_TOKEN`) |
| `table_names` | No | Table names or IDs to load (empty = all) |

**Pip dependency:** `pyairtable` (installed automatically)

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- No incremental loading — full refresh on each sync
