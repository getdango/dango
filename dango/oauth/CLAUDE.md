# oauth/

## Purpose

Manages browser-based OAuth authentication flows, provider-specific token exchange, credential persistence, and live token validation for dlt data sources.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | OAuth flow orchestration and local callback server | `OAuthManager`, `OAuthCallbackHandler`, `create_oauth_manager`, re-exports from `validation.py` |
| `providers.py` | Provider-specific OAuth implementations | `BaseOAuthProvider`, `GoogleOAuthProvider`, `FacebookOAuthProvider`, `ShopifyOAuthProvider` |
| `router.py` | Routes OAuth requests to correct provider | `run_oauth_for_source`, `check_oauth_credentials_exist`, `OAUTH_PROVIDER_MAP` |
| `storage.py` | Token persistence to `.dlt/secrets.toml` with metadata. `OAuthCredential` includes health methods: `is_expired()`, `days_until_expiry()`, `is_expiring_soon()` | `OAuthStorage`, `OAuthCredential` |
| `validation.py` | Live token validation and refresh checking via API calls | `TokenValidationResult`, `validate_token`, `validate_all_tokens`, `validate_before_sync`, `validate_google_token`, `validate_facebook_token`, `validate_shopify_token` |
| `web_flow.py` | Browser-based OAuth token exchange for cloud deployments | `OAuthFlowError`, `SUPPORTED_OAUTH_SOURCES`, `build_google_auth_url()`, `exchange_google_code()`, `fetch_google_user_info()`, `build_facebook_auth_url()`, `exchange_facebook_code()` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new OAuth provider | `providers.py` (new class), `router.py` (`OAUTH_PROVIDER_MAP`), `validation.py` (add validator) | Manual: `dango add` and select new source type |
| Change token storage format | `storage.py` | Manual: verify `.dlt/secrets.toml` after auth |
| Change callback server behavior | `__init__.py` (`OAuthCallbackHandler`) | Manual: run OAuth flow and check callback |
| Check credential existence logic | `router.py` (`check_oauth_credentials_exist`) | Manual: `dango validate` after auth |
| Add a new token validator | `validation.py` (new function + add to `_PROVIDER_VALIDATORS`) | `pytest tests/unit/test_oauth_validation.py` |
| Test live token validation | `validation.py` | `pytest tests/unit/test_oauth_validation.py` |

## Dependencies

**Imports from:**
- `dango/config/credentials.py` — `CredentialManager` for reading/writing `.dlt/secrets.toml`
- `dango/exceptions.py` — `OAuthTokenRevokedError`, `OAuthTokenExpiredError`
- `requests` — HTTP calls in `validation.py` for live token checks

**Used by:**
- `dango/cli/commands/oauth.py` — OAuth management commands (`dango oauth check` uses `validate_all_tokens`)
- `dango/cli/commands/source.py` — pre-sync validation (`validate_before_sync`)
- `dango/cli/commands/platform.py` — token health in `dango status`
- `dango/cli/source_wizard.py` — inline OAuth during `dango add`
- `dango/cli/validate.py` — credential validation
- `dango/ingestion/dlt_runner.py` — `OAuthStorage` for token expiry checks before sync
- `dango/web/routes/health.py` — OAuth token health in `/api/health/platform`
- `dango/web/routes/secrets.py` — OAuth credential management (admin-only)
- `dango/web/routes/oauth_connect.py` — web-based OAuth connect/callback
- `dango/web/routes/sync.py` — pre-sync validation before web-triggered syncs

## Testing

- **Unit:** `pytest tests/unit/test_oauth_validation.py` (26 tests covering all validators, routing, pre-sync gate)
- **Integration:** None yet (will be `tests/integration/test_oauth.py`)
- **Manual:** `dango oauth check` (live validation), `dango status` (token health)

## Don't Modify

| File | Reason |
|------|--------|
| `providers.py` OAuth endpoints and scopes | Set by third-party APIs (Google, Facebook, Shopify); changes break authentication |
| `storage.py` credential key structure | Must match dlt's expected secrets.toml format (e.g., `sources.{type}.credentials` for Google, flat keys for others) |
| `router.py` `OAUTH_PROVIDER_MAP` keys | Source type keys must match `SourceType` enum values in `config/models.py` |
| `validation.py` network error policy | Network errors return `valid=True` by design — don't block sync on transient API failures |
