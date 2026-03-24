# GitHub

**Category:** Development | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Generate a classic token with scopes: `repo`, `read:org`, `read:user`
3. Copy the token
4. Run `dango source add`, select **GitHub**, and enter the token, owner, and repo name

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `access_token_env` | Yes | GitHub Personal Access Token (env var: `GITHUB_ACCESS_TOKEN`) |
| `owner` | Yes | Repository owner (e.g., `getdango`) |
| `name` | Yes | Repository name (e.g., `dango`) |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
- The dlt source (`github_reactions`) accepts the access token as a function parameter
