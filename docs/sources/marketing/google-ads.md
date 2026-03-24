# Google Ads

**Category:** Marketing & Analytics | **Auth:** OAuth | **Wizard:** Yes

## Setup

1. Run `dango source add` and select **Google Ads**
2. The wizard triggers Google OAuth — browser opens for consent
3. Grant access to your Google Ads data
4. The OAuth flow collects your `customer_id` automatically

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `customer_id` | Yes | Collected automatically during OAuth |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Google OAuth tokens are refreshed automatically by dlt
- `customer_id` is an OAuth-collected parameter — skipped at runtime (see [OAuth Pattern](../oauth-pattern.md))
