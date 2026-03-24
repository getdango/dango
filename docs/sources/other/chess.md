# Chess.com

**Category:** Other | **Auth:** None | **Wizard:** Yes

## What Data You Get

- Player game archives, profiles, and statistics
- **Incremental:** No — full refresh on each sync (but fast due to public API)
- Tested with real data: 67,815 rows on full sync, 4 rows on second sync

## Setup

1. No authentication needed — Chess.com API is public
2. Run `dango source add`, select **Chess.com**, and enter player usernames

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `players` | Yes | Player usernames to track (comma-separated) |

## Known Limitations

- Public API with rate limiting — large numbers of players may be slow
- Good source for testing Dango without needing API keys
