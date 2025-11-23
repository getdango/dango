# Multi-Account OAuth Support (In Progress)

## Current Status: PARTIALLY IMPLEMENTED

### What Works
- Source wizard now calls OAuth **after** getting source name
- Router function updated to accept `source_name` parameter
- Credential checking updated to look for instance-specific credentials

### What's Needed
OAuth providers (GoogleOAuthProvider, FacebookOAuthProvider, ShopifyOAuthProvider) need to:
1. Accept optional `source_name` parameter in `authenticate()` method
2. When `source_name` provided, save to `[sources.{source_name}]` instead of `[sources.{provider}]`
3. When `source_name` not provided (standalone `dango auth`), save to `[sources.{provider}]` (shared)

## Use Cases

### Use Case 1: Single Account (Works Today)
```bash
# Standalone auth (saves to [sources.google_ads])
dango auth google_ads

# Add source (uses shared credentials)
dango source add → google_sheets_sales

# Result: google_sheets_sales uses [sources.google_ads] credentials
```

### Use Case 2: Multiple Accounts (Needs Provider Updates)
```bash
# Add first account
dango source add
→ Select "Facebook Ads"
→ Name: "facebook_us"
→ OAuth inline: Saves to [sources.facebook_us] ✓ (router updated)
→ BUT: Provider still saves to [sources.facebook_ads] ❌ (needs update)

# Add second account
dango source add
→ Select "Facebook Ads"
→ Name: "facebook_eu"
→ OAuth inline: Should save to [sources.facebook_eu]
→ BUT: Provider still saves to [sources.facebook_ads] ❌ (overwrites!)
```

## Required Changes

### 1. Update GoogleOAuthProvider.authenticate()

```python
def authenticate(self, service: str = "google_ads", source_name: Optional[str] = None) -> bool:
    # ... OAuth flow ...

    # Determine save location
    save_key = source_name if source_name else service

    # Save credentials to instance-specific or shared section
    self.oauth_manager.save_oauth_credentials(save_key, credentials)
```

### 2. Update FacebookOAuthProvider.authenticate()

```python
def authenticate(self, source_name: Optional[str] = None) -> bool:
    # ... OAuth flow ...

    # Determine save location
    save_key = source_name if source_name else "facebook_ads"

    # Save credentials
    self.oauth_manager.save_oauth_credentials(save_key, credentials)
```

### 3. Update ShopifyOAuthProvider.authenticate()

```python
def authenticate(self, source_name: Optional[str] = None) -> bool:
    # ... token collection ...

    # Determine save location
    save_key = source_name if source_name else "shopify"

    # Save credentials
    self.oauth_manager.save_oauth_credentials(save_key, credentials)
```

## Expected Behavior After Fix

### .dlt/secrets.toml with Multiple Accounts

```toml
# Shared credentials (from standalone dango auth)
[sources.google_ads]
client_id = "xxx.apps.googleusercontent.com"
client_secret = "GOCSPX-shared"
refresh_token = "1//shared"

# Instance-specific credentials (from inline OAuth during source add)
[sources.facebook_us]
access_token = "EAAus_token"
account_id = "act_123"

[sources.facebook_eu]
access_token = "EAAeu_token"
account_id = "act_456"

[sources.shopify_us]
private_app_password = "shpat_us"
shop_url = "us-store.myshopify.com"

[sources.shopify_eu]
private_app_password = "shpat_eu"
shop_url = "eu-store.myshopify.com"
```

## Testing Plan

1. **Single account flow** (should still work):
   ```bash
   dango auth google_ads
   dango source add → google_sheets_data
   # Should use [sources.google_ads] credentials
   ```

2. **Multi-account flow** (fix target):
   ```bash
   dango source add → facebook_us → OAuth inline
   dango source add → facebook_eu → OAuth inline
   # Should create [sources.facebook_us] and [sources.facebook_eu]
   ```

3. **Mixed flow**:
   ```bash
   dango auth google_ads  # Shared credentials
   dango source add → google_sheets_us  # Uses shared
   dango source add → google_sheets_eu  # Uses shared
   # All use [sources.google_ads]
   ```

## Priority
**HIGH** - Blocks multi-account setups for Facebook Ads, Google Ads, etc.

---
*Created: Nov 24, 2024*
*Status: In Progress - Router updated, providers need update*
