# OAuth Authentication Setup Guide

This guide explains how to authenticate with OAuth-enabled data sources in Dango.

## Overview

Dango supports OAuth authentication for the following data sources:
- **Google Ads** - Advertising campaigns and performance data
- **Google Analytics (GA4)** - Website analytics
- **Google Sheets** - Spreadsheet data
- **Facebook/Meta Ads** - Social media advertising data
- **Shopify** - E-commerce store data (Custom App tokens)

All OAuth credentials are stored securely in `.dlt/secrets.toml` (gitignored) and use refresh tokens for long-term access.

## Privacy First: Why You Create Your Own OAuth App

Dango asks you to create your own OAuth apps for **maximum privacy**:

- **Your data flows directly:** Provider → Your Machine → Your Local Database
- **Dango never touches your data** (no intermediary servers)
- **You control the OAuth app** and can revoke access anytime
- **No shared rate limits or quotas** with other users

This aligns with Dango's local-first, privacy-focused architecture.

### Understanding "External" OAuth Type

When setting up Google OAuth, you'll select "External" user type. This refers to **WHO can authenticate**, NOT where your data goes:

- **External** = Any Google account (@gmail.com, @company.com)
- **Internal** = Only your organization (@yourcompany.com) - requires Google Workspace

Your data stays local regardless of this setting.

## Check Your OAuth Configuration

Before starting, you can check your current OAuth status:

```bash
# Check OAuth credentials and status
dango auth check

# List saved OAuth tokens
dango auth-list

# Interactive setup wizard
dango auth setup google
```

## Quick Start (Two Methods)

### Method 1: Inline OAuth (Recommended)

OAuth setup happens **automatically during source configuration**:

1. **Initialize your Dango project** (if not already done):
   ```bash
   dango init my-project
   cd my-project
   ```

2. **Add a source** (OAuth setup will run automatically):
   ```bash
   dango source add
   ```

3. **Select an OAuth source** (e.g., Google Sheets, Facebook Ads)
   - Dango detects OAuth requirement
   - Prompts you to authenticate inline
   - Opens browser for OAuth flow
   - Continues with source configuration

4. **Sync your data**:
   ```bash
   dango sync
   ```

### Method 2: Separate OAuth Setup

Authenticate first, then add sources:

1. **Run the auth command** for your data source:
   ```bash
   dango auth <provider>
   ```

2. **Follow the browser OAuth flow**

3. **Add the source** to your project:
   ```bash
   dango source add
   ```
   - OAuth credentials already configured
   - Wizard will detect existing credentials
   - Skip straight to source configuration

4. **Sync your data**:
   ```bash
   dango sync
   ```

**When to use each method:**
- **Inline** (Method 1): First-time setup, single source
- **Separate** (Method 2): Re-authentication, multiple sources sharing credentials, troubleshooting

## Provider-Specific Setup

### Google Services (Ads, Analytics, Sheets)

Google services share the same OAuth credentials. You authenticate once with Google, selecting which service(s) you need.

#### Common Setup (All Google Services)

**Prerequisites:**
- Google Cloud Project
- OAuth 2.0 Web Application credentials

**Create OAuth credentials:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create/select a project
3. Enable the required APIs:
   - **Google Ads:** Enable "Google Ads API"
   - **Google Analytics:** Enable "Google Analytics Data API"
   - **Google Sheets:** Enable "Google Sheets API" and "Google Drive API"
4. Go to APIs & Services > Credentials
5. Create OAuth client ID → **Web application** (NOT Desktop app)
6. Add authorized redirect URI: `http://localhost:8080/callback`
7. Download/copy Client ID and Client Secret

**Tip:** Add credentials to `.env` to skip prompts:
```bash
GOOGLE_CLIENT_ID=123456789.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxx
```

---

#### Google Ads

**Additional Prerequisites:**
- Google Ads Developer Token
- Customer ID

**Run authentication:**
```bash
dango auth google --service ads
```

**During the flow:**
- Enter Client ID and Client Secret (or auto-detected from .env)
- Browser opens → Sign in with Google → Grant permissions
- Enter Developer Token when prompted
- Enter Customer ID (optional, can add later)

**Get Developer Token:** [Google Ads API Center](https://ads.google.com/aw/apicenter)

**Expiry:** Refresh token doesn't expire (permanent access)

---

#### Google Analytics (GA4)

**Additional Prerequisites:**
- GA4 Property ID

**Run authentication:**
```bash
dango auth google --service analytics
```

**During the flow:**
- Enter Client ID and Client Secret (or auto-detected from .env)
- Browser opens → Sign in → Grant permissions
- Enter GA4 Property ID when prompted

**Expiry:** Refresh token doesn't expire

---

#### Google Sheets

**Run authentication:**
```bash
dango auth google --service sheets
```

**During the flow:**
- Enter Client ID and Client Secret (or auto-detected from .env)
- Browser opens → Sign in → Grant permissions
- Spreadsheet IDs are selected when adding sources

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
1. **Stop the conflicting process:**
   ```bash
   # Find what's using port 8080
   lsof -i :8080

   # Kill the process
   kill <PID>
   ```

2. **Or use a different port:**
   Add to your `.env` file:
   ```bash
   DANGO_OAUTH_CALLBACK_URL=http://localhost:8081/callback
   ```
   Then update your OAuth app's redirect URI to match.

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
