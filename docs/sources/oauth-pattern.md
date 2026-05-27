# OAuth Credential Pattern

## Overview

Dango uses OAuth for Google (Ads, Analytics, Sheets) and Facebook Ads. The canonical pattern:

1. **Wizard prompts for OAuth** during `dango source add`
2. **Browser opens** for user consent
3. **Credentials saved** to `.dlt/secrets.toml` under `[sources.{source_name}]`
4. **dlt auto-refreshes** Google tokens on each sync (Facebook requires manual refresh every 60 days)

## How It Works

OAuth-collected parameters (e.g., `customer_id` for Google Ads) stay in the registry's `required_params` list but are **skipped at runtime** — the wizard knows they were already collected during the OAuth flow.

The reference implementation is `facebook_ads` in `dango/oauth/providers.py`.

## Per-Source Details

### Google (Ads, Analytics, Sheets)

- **Tokens:** `client_id`, `client_secret`, `refresh_token` → `.dlt/secrets.toml`
- **Refresh:** Automatic. dlt refreshes the access token on each pipeline run.
- **Scopes:** Configured per service in `GoogleOAuthProvider`.

### Facebook Ads

- **Tokens:** Short-lived token exchanged for long-lived (60-day) token.
- **Refresh:** Manual. Run `dango oauth refresh <credential_name>` before expiry.
- **Warning:** Dango shows a console warning when the token is within 7 days of expiry.

## Multi-Account Support

Each source instance gets its own credentials section in `.dlt/secrets.toml`:

```toml
[sources.facebook_us]
access_token = "..."
account_id = "123456789"

[sources.facebook_eu]
access_token = "..."
account_id = "987654321"
```

## Credential Storage

| Provider | Storage Location | Key Fields |
|----------|-----------------|------------|
| Google | `.dlt/secrets.toml` | `client_id`, `client_secret`, `refresh_token` |
| Facebook | `.dlt/secrets.toml` | `access_token` |

Credentials are never stored in `sources.yml` or `.env` — only in `.dlt/secrets.toml`.
