# Facebook Ads

**Category:** Marketing & Analytics | **Auth:** OAuth | **Wizard:** Yes

## What Data You Get

- **Entity data:** Campaigns, ad sets, ads, creatives with full metadata
- **Performance metrics:** Impressions, clicks, spend, conversions by day
- **Incremental:** Yes — subsequent syncs fetch only new data

## Setup

1. Run `dango source add` and select **Facebook Ads**
2. The wizard triggers OAuth — a browser window opens for Facebook login
3. Paste the short-lived token when prompted
4. Enter your Facebook App ID and App Secret
5. The wizard exchanges for a 60-day long-lived token
6. Enter your Ad Account ID (numeric, e.g., `156434076`)

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `account_id` | Yes | Facebook Ads Account ID (numeric, no `act_` prefix) |
| `access_token_env` | Yes | Collected via OAuth flow |
| `initial_load_past_days` | No | Days of historical data on first sync (default: 30) |

**Pip dependency:** `facebook-business` (installed automatically)

## Known Limitations

- **60-day token expiry.** Run `dango oauth refresh <credential>` before expiry. Dango warns at 7 days.
- **Account ID format:** The wizard strips `act_` prefix automatically. The dlt source prepends it internally.
- OAuth-collected parameters stay in `required_params` but are skipped at runtime (see [OAuth Pattern](../oauth-pattern.md)).

## Troubleshooting

| Error | Fix |
|-------|-----|
| `act_act_` double prefix | Fixed in Phase 5. Wizard now strips `act_` before saving. |
| Token expired | Run `dango oauth refresh <credential_name>` |
| Permission denied | Verify your Facebook App has `ads_read` scope |
