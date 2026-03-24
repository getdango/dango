# Mux

**Category:** Marketing & Analytics | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Log in to Mux
2. Go to **Settings > Access Tokens**
3. Create a new token with read permissions
4. Add credentials to `.dlt/secrets.toml`:
   ```toml
   [sources.mux.credentials]
   token_id = "your-token-id"
   token_secret = "your-token-secret"
   ```
5. Run `dango source add` and select **Mux**

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `start_date` | No | Start date for video views |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Credentials go in `.dlt/secrets.toml` (uses `dlt.secrets.value` decorator pattern — not function parameters)
- No incremental loading — full refresh on each sync
- Performance metrics supported (video views, engagement)
