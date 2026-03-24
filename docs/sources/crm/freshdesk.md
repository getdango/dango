# Freshdesk

**Category:** Business & CRM | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Log in to Freshdesk as admin
2. Go to **Profile Settings**
3. Copy your API key
4. Run `dango source add`, select **Freshdesk**, and enter your domain and API key

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `domain` | Yes | Freshdesk domain (e.g., `yourcompany`) |
| `api_secret_key_env` | Yes | Freshdesk API Key (env var: `FRESHDESK_API_KEY`) |
| `endpoints` | No | Resources to sync (default: all) |
| `per_page` | No | Results per page, max 100 (default: 100) |

Available resources: agents, companies, contacts, groups, roles, tickets, time_entries

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
