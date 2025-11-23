# Advanced Usage Guide

This guide covers advanced Dango features for power users who want maximum flexibility and control.

## Table of Contents

1. [Registry Bypass (dlt_native)](#registry-bypass-dlt_native)
2. [Custom dlt Sources](#custom-dlt-sources)
3. [Using Unofficial dlt Sources](#using-unofficial-dlt-sources)
4. [Advanced OAuth Configuration](#advanced-oauth-configuration)
5. [Multi-Instance Sources](#multi-instance-sources)
6. [Direct .dlt/ Configuration](#direct-dlt-configuration)

---

## Registry Bypass (dlt_native)

The `dlt_native` source type allows you to use **any dlt source** without it being in Dango's registry. This is useful for:

- Using dlt verified sources not yet in Dango's registry
- Creating custom dlt sources tailored to your needs
- Prototyping new integrations quickly

### How It Works

1. **Create source**: Place Python files in `custom_sources/` directory
2. **Configure**: Edit `.dango/sources.yml` with `type: dlt_native`
3. **Sync**: Run `dango sync --source <name>` as normal

### Example: Custom API Source

**1. Create `custom_sources/shopee_api.py`:**

```python
import dlt
from dlt.sources.helpers import requests

@dlt.source
def shopee_source(
    partner_id: int = dlt.secrets.value,
    partner_key: str = dlt.secrets.value,
    shop_id: int = dlt.secrets.value
):
    """Load data from Shopee Open Platform API"""

    @dlt.resource(write_disposition="merge", primary_key="order_sn")
    def orders(timestamp: int = dlt.sources.incremental("timestamp")):
        """Load orders with incremental loading"""
        url = "https://partner.shopeemobile.com/api/v2/order/get_order_list"

        # Shopee API signature generation
        import time, hmac, hashlib
        ts = int(time.time())
        path = "/api/v2/order/get_order_list"
        base_string = f"{partner_id}{path}{ts}"
        sign = hmac.new(
            partner_key.encode(),
            base_string.encode(),
            hashlib.sha256
        ).hexdigest()

        params = {
            "partner_id": partner_id,
            "timestamp": ts,
            "sign": sign,
            "shop_id": shop_id,
            "time_from": timestamp.last_value or 0,
            "time_to": int(time.time()),
            "page_size": 100
        }

        response = requests.get(url, params=params)
        data = response.json()

        for order in data.get("response", {}).get("order_list", []):
            yield order

    @dlt.resource(write_disposition="merge", primary_key="item_id")
    def products():
        """Load product catalog"""
        # Similar implementation
        pass

    return orders, products
```

**2. Configure in `.dango/sources.yml`:**

```yaml
sources:
  - name: shopee_sg
    type: dlt_native
    enabled: true
    description: "Shopee Singapore store data"
    dlt_native:
      source_module: shopee_api  # Looks in custom_sources/shopee_api.py
      source_function: shopee_source
      function_kwargs: {}  # Credentials from .dlt/secrets.toml
```

**3. Add credentials to `.dlt/secrets.toml`:**

```toml
[sources.shopee_sg]
partner_id = 123456
partner_key = "your_partner_key_here"
shop_id = 789012
```

**4. Sync:**

```bash
dango sync --source shopee_sg
```

---

## Custom dlt Sources

### Best Practices

#### 1. **Use dlt Decorators**

Always use `@dlt.source` and `@dlt.resource`:

```python
@dlt.source
def my_source():
    @dlt.resource(write_disposition="merge", primary_key="id")
    def my_resource():
        yield {"id": 1, "data": "example"}

    return my_resource
```

#### 2. **Incremental Loading**

Use dlt's incremental loading for efficient syncs:

```python
@dlt.resource(write_disposition="merge", primary_key="id")
def users(updated_at: dlt.sources.incremental("updated_at") = None):
    # Only fetch records newer than last run
    since = updated_at.last_value or "2020-01-01"

    # Your API call with since parameter
    records = api.get_users(since=since)

    for record in records:
        yield record
```

#### 3. **Secret Management**

Use `dlt.secrets.value` for credentials:

```python
@dlt.source
def my_api(api_key: str = dlt.secrets.value):
    # api_key will be loaded from .dlt/secrets.toml
    pass
```

#### 4. **Error Handling**

Handle API errors gracefully:

```python
@dlt.resource
def robust_resource():
    import time
    retries = 3

    for attempt in range(retries):
        try:
            response = requests.get(url)
            response.raise_for_status()
            yield response.json()
            break
        except requests.HTTPError as e:
            if e.response.status_code == 429:  # Rate limit
                time.sleep(60)
                continue
            raise
```

---

## Using Unofficial dlt Sources

You can use any dlt verified source, even if not in Dango's registry.

### Example: Zendesk (Not in Registry)

**1. Install dlt source:**

```bash
pip install dlt[zendesk]
```

**2. Configure in `.dango/sources.yml`:**

```yaml
sources:
  - name: zendesk_support
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: zendesk  # Installed dlt package
      source_function: zendesk_support
      function_kwargs:
        subdomain: mycompany
        start_date: "2024-01-01"
```

**3. Add credentials to `.dlt/secrets.toml`:**

```toml
[sources.zendesk_support.credentials]
email = "support@mycompany.com"
token = "your_zendesk_api_token"
```

**4. Sync:**

```bash
dango sync --source zendesk_support
```

---

## Advanced OAuth Configuration

### Manual OAuth Token Management

For sources requiring OAuth, you can manually manage tokens in `.dlt/secrets.toml`.

#### Google Services (Ads, Analytics, Sheets)

After running `dango auth google_<service>`, credentials are stored as:

```toml
[sources.google_ads]
client_id = "123456789.apps.googleusercontent.com"
client_secret = "GOCSPX-xxxxxxxxxxxxx"
refresh_token = "1//xxxxxxxxxxxxx"
developer_token = "xxxxxxxxxxxxx"
customer_id = "1234567890"
```

You can:
- Copy these to other projects
- Share with team members (via secure channels)
- Backup and version control (encrypted)

#### Facebook Ads

Long-lived tokens expire in 60 days. Set a reminder:

```bash
# Check token expiry
cat .dlt/secrets.toml | grep -A 5 "facebook_ads"

# Re-authenticate before expiry
dango auth facebook_ads
```

### Sharing OAuth Credentials Across Sources

Google OAuth credentials can be shared across multiple Google services:

```toml
# Single OAuth credentials for all Google services
[sources.google_ads]
client_id = "xxx.apps.googleusercontent.com"
client_secret = "GOCSPX-xxx"
refresh_token = "1//xxx"

[sources.google_analytics]
# Same credentials - dlt will find them
# No need to duplicate

[sources.google_sheets]
# Same credentials - dlt will find them
```

---

## Multi-Instance Sources

Run multiple instances of the same source (e.g., multiple Shopify stores, Stripe accounts).

### Example: Multiple Shopify Stores

**`.dango/sources.yml`:**

```yaml
sources:
  - name: shopify_us
    type: shopify
    enabled: true
    shopify:
      shop_url: us-store.myshopify.com
      api_key_env: SHOPIFY_US_ACCESS_TOKEN

  - name: shopify_eu
    type: shopify
    enabled: true
    shopify:
      shop_url: eu-store.myshopify.com
      api_key_env: SHOPIFY_EU_ACCESS_TOKEN
```

**`.dlt/secrets.toml`:**

```toml
[sources.shopify_us]
private_app_password = "shpat_us_xxxxx"
shop_url = "us-store.myshopify.com"

[sources.shopify_eu]
private_app_password = "shpat_eu_xxxxx"
shop_url = "eu-store.myshopify.com"
```

**Result:**
- `raw_shopify_us.orders`
- `raw_shopify_us.customers`
- `raw_shopify_eu.orders`
- `raw_shopify_eu.customers`

---

## Direct .dlt/ Configuration

Advanced users can bypass Dango configuration and use dlt native config.

### `.dlt/config.toml` - Non-Secret Settings

```toml
[sources.my_source]
# Non-secret configuration
batch_size = 1000
timeout = 30
start_date = "2024-01-01"
```

### `.dlt/secrets.toml` - Sensitive Data

```toml
[sources.my_source]
# Secrets - gitignored
api_key = "xxxxx"
username = "admin"
password = "secret"
```

### Environment Variables

dlt also supports environment variables:

```bash
# .env
MY_SOURCE__API_KEY=xxxxx
MY_SOURCE__BATCH_SIZE=1000
```

### Priority Order

1. `.dlt/secrets.toml` (highest)
2. `.dlt/config.toml`
3. Environment variables (`.env`)
4. Code defaults (lowest)

---

## Troubleshooting

### Import Errors

```
ModuleNotFoundError: No module named 'my_source'
```

**Fix:**
- Ensure file is in `custom_sources/` directory
- Check filename matches `source_module` in config
- Restart any running Dango processes

### Credential Errors

```
KeyError: 'api_key'
```

**Fix:**
- Check `.dlt/secrets.toml` has correct section: `[sources.my_source]`
- Verify credential names match your source function parameters
- Use `dlt.secrets.value` decorator in Python code

### Pipeline State Issues

```
PipelineStateEngineError: State is corrupted
```

**Fix:**
```bash
# Full refresh (drops state and reloads)
dango sync --source my_source --full-refresh
```

---

## Additional Resources

- **dlt Documentation**: https://dlthub.com/docs
- **dlt Verified Sources**: https://dlthub.com/docs/dlt-ecosystem/verified-sources
- **Build a Pipeline Tutorial**: https://dlthub.com/docs/build-a-pipeline-tutorial
- **dlt Community**: https://dlthub.com/community

---

## Getting Help

1. **Check Logs**: `tail -f .dango/activity.log`
2. **Validate Config**: `dango config validate`
3. **Test dlt Source**: Run source function directly in Python
4. **GitHub Issues**: https://github.com/getdango/dango/issues

---

*Last updated: Nov 2024*
