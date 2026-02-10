# oauth/

## Purpose

Manages browser-based OAuth authentication flows, provider-specific token exchange, and credential persistence for dlt data sources.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | OAuth flow orchestration and local callback server | `OAuthManager`, `OAuthCallbackHandler`, `create_oauth_manager` |
| `providers.py` | Provider-specific OAuth implementations | `BaseOAuthProvider`, `GoogleOAuthProvider`, `FacebookOAuthProvider`, `ShopifyOAuthProvider` |
| `router.py` | Routes OAuth requests to correct provider | `run_oauth_for_source`, `check_oauth_credentials_exist`, `OAUTH_PROVIDER_MAP` |
| `storage.py` | Token persistence to `.dlt/secrets.toml` with metadata | `OAuthStorage`, `OAuthCredential` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new OAuth provider | `providers.py` (new class), `router.py` (`OAUTH_PROVIDER_MAP`) | Manual: `dango add` and select new source type |
| Change token storage format | `storage.py` | Manual: verify `.dlt/secrets.toml` after auth |
| Change callback server behavior | `__init__.py` (`OAuthCallbackHandler`) | Manual: run OAuth flow and check callback |
| Check credential existence logic | `router.py` (`check_oauth_credentials_exist`) | Manual: `dango validate` after auth |

## Dependencies

**Imports from:**
- `dango/config/credentials.py` — `CredentialManager` for reading/writing `.dlt/secrets.toml`

**Used by:**
- `dango/cli/main.py` — OAuth management commands (`dango auth`)
- `dango/cli/source_wizard.py` — inline OAuth during `dango add`
- `dango/cli/validate.py` — credential validation
- `dango/ingestion/dlt_runner.py` — `OAuthStorage` for token expiry checks before sync

## Testing

- **Unit:** None yet (will be `tests/unit/test_oauth.py`)
- **Integration:** None yet (will be `tests/integration/test_oauth.py`)
- **Manual:** `dango add` → select Google/Facebook/Shopify source → complete OAuth flow

## Don't Modify

| File | Reason |
|------|--------|
| `providers.py` OAuth endpoints and scopes | Set by third-party APIs (Google, Facebook, Shopify); changes break authentication |
| `storage.py` credential key structure | Must match dlt's expected secrets.toml format (e.g., `sources.{type}.credentials` for Google, flat keys for others) |
| `router.py` `OAUTH_PROVIDER_MAP` keys | Source type keys must match `SourceType` enum values in `config/models.py` |
