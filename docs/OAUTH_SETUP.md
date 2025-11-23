# OAuth Authentication Setup Guide

This guide explains how to authenticate with OAuth-enabled data sources in Dango.

## Overview

Dango supports OAuth authentication for the following data sources:
- **Google Ads** - Advertising campaigns and performance data
- **Google Analytics (GA4)** - Website analytics
- **Google Sheets** - Spreadsheet data
- **Facebook/Meta Ads** - Social media advertising data
- **Shopify** - E-commerce store data

All OAuth credentials are stored securely in `.dlt/secrets.toml` (gitignored) and use refresh tokens for long-term access.

## Quick Start

The general workflow is:

1. **Initialize your Dango project** (if not already done):
   ```bash
   dango init my-project
   cd my-project
   ```

2. **Run the auth command** for your data source:
   ```bash
   dango auth <provider>
   ```

3. **Follow the browser OAuth flow** - Dango will open your browser automatically

4. **Add the source** to your project:
   ```bash
   dango source add
   ```

5. **Sync your data**:
   ```bash
   dango sync
   ```

## Provider-Specific Setup

### Google Ads

**Prerequisites:**
- Google Cloud Project with Google Ads API enabled
- OAuth 2.0 Web Application credentials
- Google Ads Developer Token
- Customer ID

**Steps:**

1. **Create OAuth credentials:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create/select a project
   - Enable "Google Ads API"
   - Go to APIs & Services > Credentials
   - Create OAuth client ID → Web application
   - Add authorized redirect URI: `http://localhost:8080/callback`
   - Download/copy Client ID and Client Secret

2. **Get Developer Token:**
   - Go to [Google Ads API Center](https://ads.google.com/aw/apicenter)
   - Apply for and receive your Developer Token

3. **Run authentication:**
   ```bash
   dango auth google_ads
   ```

4. **During the flow:**
   - Enter Client ID and Client Secret when prompted
   - Browser opens → Sign in with Google → Grant permissions
   - Enter Developer Token when prompted
   - Enter Customer ID (optional, can add later)

**Credentials stored in:** `.dlt/secrets.toml` under `sources.google_ads`

**Expiry:** Refresh token doesn't expire (permanent access)

---

### Google Analytics (GA4)

**Prerequisites:**
- Google Cloud Project with Google Analytics Data API enabled
- OAuth 2.0 Web Application credentials
- GA4 Property ID

**Steps:**

1. **Create OAuth credentials:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create/select a project
   - Enable "Google Analytics Data API"
   - Create OAuth client ID → Web application
   - Add authorized redirect URI: `http://localhost:8080/callback`

2. **Run authentication:**
   ```bash
   dango auth google_analytics
   ```

3. **During the flow:**
   - Enter Client ID and Client Secret
   - Browser opens → Sign in → Grant permissions
   - Enter GA4 Property ID when prompted

**Credentials stored in:** `.dlt/secrets.toml` under `sources.google_analytics`

**Expiry:** Refresh token doesn't expire

---

### Google Sheets

**Prerequisites:**
- Google Cloud Project with Google Sheets API enabled
- OAuth 2.0 Web Application credentials
- Spreadsheet ID(s)

**Steps:**

1. **Create OAuth credentials:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Enable "Google Sheets API" and "Google Drive API"
   - Create OAuth client ID → Web application
   - Add authorized redirect URI: `http://localhost:8080/callback`

2. **Run authentication:**
   ```bash
   dango auth google_sheets
   ```

3. **During the flow:**
   - Enter Client ID and Client Secret
   - Browser opens → Sign in → Grant permissions
   - Spreadsheet IDs can be added later when creating sources

**Credentials stored in:** `.dlt/secrets.toml` under `sources.google_sheets`

**Expiry:** Refresh token doesn't expire

---

### Facebook/Meta Ads

**Prerequisites:**
- Facebook Developer account
- Facebook App (Business type)
- Ad Account ID

**Steps:**

1. **Create Facebook App:**
   - Go to [Facebook Developers](https://developers.facebook.com/)
   - Create new app → Business type
   - Add "Marketing API" product

2. **Run authentication:**
   ```bash
   dango auth facebook_ads
   ```

3. **During the flow:**
   - Browser opens → Facebook Graph API Explorer
   - Generate short-lived access token with `ads_read`, `ads_management` permissions
   - Copy token and paste in terminal
   - Enter App ID and App Secret
   - Dango exchanges for 60-day long-lived token
   - Enter Ad Account ID (e.g., `act_123456789`)

**Credentials stored in:** `.dlt/secrets.toml` under `sources.facebook_ads`

**Expiry:** 60 days - you'll need to re-authenticate

**Important:** Set a calendar reminder to re-run `dango auth facebook_ads` before the token expires.

---

### Shopify

**Prerequisites:**
- Shopify store (any plan)
- Admin access to create custom apps

**Steps:**

1. **Create Custom App:**
   - Go to Shopify Admin → Settings
   - Apps and sales channels → Develop apps
   - Create an app
   - Configure Admin API scopes (read permissions for data you need)
   - Install the app
   - Reveal Admin API access token

2. **Run authentication:**
   ```bash
   dango auth shopify
   ```

3. **During the flow:**
   - Enter shop URL (e.g., `mystore.myshopify.com`)
   - Enter Admin API access token
   - Dango tests the connection

**Credentials stored in:** `.dlt/secrets.toml` under `sources.shopify`

**Expiry:** Permanent (no expiry)

---

## Credential Storage

### .dlt/ Directory Structure

Dango uses dlt's native credential format:

```
your-project/
├── .dlt/
│   ├── secrets.toml      # OAuth tokens and secrets (gitignored)
│   └── config.toml       # Non-sensitive config (can be committed)
├── .env                  # Legacy format (still supported)
└── ...
```

### Example `.dlt/secrets.toml`

```toml
[sources.google_ads]
client_id = "123456789.apps.googleusercontent.com"
client_secret = "GOCSPX-xxxxxxxxxxxxx"
refresh_token = "1//xxxxxxxxxxxxx"
developer_token = "xxxxxxxxxxxxx"
customer_id = "1234567890"

[sources.facebook_ads]
access_token = "EAAxxxxxxxxxx"
account_id = "act_123456789"

[sources.shopify]
private_app_password = "shpat_xxxxxxxxxxxxx"
shop_url = "mystore.myshopify.com"
```

### Credential Priority

When loading credentials, Dango uses this priority:
1. **`.dlt/secrets.toml`** (highest priority - recommended)
2. **`.env`** file (fallback for backward compatibility)

---

## Troubleshooting

### "No refresh token received" (Google OAuth)

**Problem:** Google doesn't return a refresh token.

**Solution:**
- Go to [Google Account Permissions](https://myaccount.google.com/permissions)
- Revoke access for your Dango app
- Run `dango auth <provider>` again
- Ensure you see the consent screen (not just "Sign in")

### "Redirect URI mismatch"

**Problem:** OAuth callback fails with redirect URI error.

**Solution:**
- Ensure you created a **Web application** (not Desktop app)
- Add exactly: `http://localhost:8080/callback` to authorized redirect URIs
- No trailing slash, must be http (not https)

### "Port 8080 already in use"

**Problem:** OAuth callback server can't start.

**Solution:**
- Stop any process using port 8080
- Or edit `dango/oauth/__init__.py` and change `self.callback_port = 8080` to another port

### Facebook token expires too quickly

**Problem:** Facebook tokens expire in 60 days.

**Solution:**
- Set a calendar reminder for 55 days from now
- Re-run `dango auth facebook_ads` before expiry
- For production, consider using a System User with a permanent token

### Credentials not loading

**Problem:** Source can't find OAuth credentials.

**Solution:**
- Ensure `.dlt/secrets.toml` exists in your project root
- Check that source name matches (e.g., `google_ads` not `google-ads`)
- Verify credentials are under `[sources.<source_name>]` section
- Run from your project directory (where `.dlt/` exists)

---

## Security Best Practices

1. **Never commit `.dlt/secrets.toml`** - It's gitignored by default
2. **Use environment-specific credentials** - Different tokens for dev/staging/prod
3. **Rotate tokens regularly** - Especially for services with expiring tokens
4. **Limit OAuth scopes** - Only request permissions you actually need
5. **Monitor token usage** - Watch for unexpected API calls
6. **Revoke unused tokens** - Clean up old credentials from cloud consoles

---

## Advanced: Manual Credential Setup

If you prefer not to use `dango auth`, you can manually add credentials to `.dlt/secrets.toml`:

1. **Create `.dlt/secrets.toml`** in your project root
2. **Add credentials** in the format shown above
3. **Verify** with `dango sync`

This is useful for:
- CI/CD pipelines
- Automated deployments
- Using credentials from secret managers
- Sharing credentials across team members (via secure channels)

---

## Getting Help

- **Documentation:** [Dango Docs](https://github.com/getdango/dango)
- **dlt Sources:** [dlt Verified Sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources)
- **Issues:** [GitHub Issues](https://github.com/getdango/dango/issues)
- **Community:** [Discord](#) (coming soon)

---

## Next Steps

Once authenticated:
1. **Add sources**: `dango source add`
2. **Configure sync**: Edit `.dango/sources.yml`
3. **Run sync**: `dango sync`
4. **Transform data**: Write dbt models in `dbt/models/`
5. **Visualize**: `dango start` → Open Metabase

Happy data engineering! 🍡
