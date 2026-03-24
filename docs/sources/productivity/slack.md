# Slack

**Category:** Communication | **Auth:** API Key | **Wizard:** Yes

## What Data You Get

- Channel messages with timestamps and user info
- **Incremental:** Yes — subsequent syncs fetch only new messages
- Tested with real data: 19 rows on initial sync

## Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
2. Add Bot Token Scopes: `channels:history`, `channels:read`, `users:read`
3. Install the app to your workspace
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
5. Invite the bot to channels you want to sync
6. Run `dango source add`, select **Slack**, and enter the token

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `access_token_env` | Yes | Slack Bot User OAuth Token (env var: `SLACK_ACCESS_TOKEN`) |
| `selected_channels` | No | Channel IDs to sync (empty = all channels the bot is in) |
| `start_date` | No | Start date for messages (default: `90daysAgo`) |

## Known Limitations

- The bot must be invited to each channel you want to sync
- Private channels require additional scopes (`groups:history`, `groups:read`)

## Troubleshooting

| Error | Fix |
|-------|-----|
| No messages synced | Invite the bot to the channel: `/invite @your-bot-name` |
| Channel not found | Use channel IDs (e.g., `C01234567`), not channel names |
