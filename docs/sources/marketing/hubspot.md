# HubSpot

**Category:** Marketing & Analytics | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Log in to your HubSpot account
2. Go to **Settings > Integrations > Private Apps**
3. Create a new private app with required scopes (contacts, companies, deals, tickets)
4. Copy the access token
5. Run `dango source add`, select **HubSpot**, and enter the token

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `api_key_env` | Yes | HubSpot API Key (env var: `HUBSPOT_API_KEY`) |
| `resources` | No | Resources to sync (default: contacts, companies, deals, tickets) |

Available resources: `contacts`, `companies`, `deals`, `tickets`, `products`, `quotes`

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
