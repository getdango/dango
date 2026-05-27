# Google Sheets

**Category:** Marketing & Analytics | **Auth:** OAuth | **Wizard:** Yes

## Setup

1. Run `dango source add` and select **Google Sheets**
2. The wizard triggers Google OAuth — browser opens for consent
3. Grant access to your Google Sheets data
4. Enter the Spreadsheet ID or URL
5. Select which sheets/tabs to load

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `spreadsheet_url_or_id` | Yes | Spreadsheet ID or full URL |
| `range_names` | Yes | Sheet names/tabs to load (selected via wizard) |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- No incremental loading — full refresh on each sync
- Google OAuth tokens are refreshed automatically by dlt
- The spreadsheet must be accessible to the OAuth account
