# Registry Bypass Guide

Learn how to use **any dlt source** in Dango, even if it's not in the official registry.

## Why Registry Bypass?

Dango's registry includes 33+ popular data sources with wizard support. But dlt supports **80+ sources** and you can write custom ones. Registry bypass (`dlt_native` source type) gives you:

✅ Access to all dlt verified sources
✅ Ability to write custom sources
✅ Full control over dlt configuration
✅ No waiting for Dango to add new sources

⚠️ **Trade-off**: No wizard support - file-based configuration only

---

## How It Works

Instead of using Dango's registry metadata, you directly specify:
- **source_module**: Which Python module contains your source
- **source_function**: Which function to call
- **function_kwargs**: What parameters to pass

Dango runs it like any other source - just without the wizard.

---

## Quick Example

Let's add the **Jira** source (not in Dango's registry yet).

### Step 1: Install dlt Source

```bash
pip install dlt[jira]
```

### Step 2: Configure in `.dango/sources.yml`

```yaml
sources:
  - name: jira_issues
    type: dlt_native
    enabled: true
    description: "Jira project issues and metadata"
    dlt_native:
      source_module: jira  # dlt package name
      source_function: jira  # Function name from dlt docs
      function_kwargs:
        subdomain: "mycompany"  # Jira subdomain
        project_keys:
          - "PROJ"
          - "TEAM"
```

### Step 3: Add Credentials to `.dlt/secrets.toml`

```toml
[sources.jira_issues.credentials]
domain = "mycompany.atlassian.net"
api_token = "your_jira_api_token"
email = "you@mycompany.com"
```

### Step 4: Sync

```bash
dango sync --source jira_issues
```

**Result:**
- `raw_jira_issues.issues`
- `raw_jira_issues.projects`
- `raw_jira_issues.users`

---

## Custom Sources

Write your own dlt source for any API or data source.

### Example: Lazada Open Platform

**Create `custom_sources/lazada_api.py`:**

```python
import dlt
from dlt.sources.helpers import requests
import hmac
import hashlib
import time

@dlt.source
def lazada_source(
    app_key: str = dlt.secrets.value,
    app_secret: str = dlt.secrets.value,
    access_token: str = dlt.secrets.value
):
    """Load data from Lazada Open Platform"""

    def sign_api_request(api_path, params):
        """Generate Lazada API signature"""
        sorted_params = sorted(params.items())
        query_string = "".join([f"{k}{v}" for k, v in sorted_params])
        sign_string = f"{api_path}{query_string}"

        signature = hmac.new(
            app_secret.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest().upper()

        return signature

    @dlt.resource(write_disposition="merge", primary_key="order_id")
    def orders(created_after: dlt.sources.incremental("created_at") = None):
        """Load orders with incremental loading"""
        api_path = "/orders/get"
        base_url = "https://api.lazada.com/rest"

        timestamp = int(time.time() * 1000)
        created_after_ts = created_after.last_value or "2024-01-01"

        params = {
            "app_key": app_key,
            "access_token": access_token,
            "timestamp": timestamp,
            "sign_method": "sha256",
            "created_after": created_after_ts,
            "limit": 100
        }

        params["sign"] = sign_api_request(api_path, params)

        response = requests.get(f"{base_url}{api_path}", params=params)
        data = response.json()

        if data.get("code") == "0":
            for order in data.get("data", {}).get("orders", []):
                yield order

    @dlt.resource(write_disposition="merge", primary_key="sku_id")
    def products():
        """Load product catalog"""
        # Similar implementation
        api_path = "/products/get"
        # ... API call logic
        pass

    return orders, products
```

**Configure in `.dango/sources.yml`:**

```yaml
sources:
  - name: lazada_sg
    type: dlt_native
    enabled: true
    description: "Lazada Singapore marketplace"
    dlt_native:
      source_module: lazada_api  # custom_sources/lazada_api.py
      source_function: lazada_source
      function_kwargs: {}  # Credentials from .dlt/secrets.toml
```

**Add credentials to `.dlt/secrets.toml`:**

```toml
[sources.lazada_sg]
app_key = "123456"
app_secret = "xxxxxxxxxxxxx"
access_token = "50000xxxxxxxxxxx"
```

**Sync:**

```bash
dango sync --source lazada_sg
```

---

## Configuration Reference

### DltNativeConfig Fields

```yaml
dlt_native:
  # Required
  source_module: "module_name"     # Python module (custom_sources/ or dlt package)
  source_function: "function_name" # Function to call

  # Optional
  function_kwargs:                 # Arguments to pass to function
    param1: "value1"
    param2: 123
    param3:
      nested: "value"

  pipeline_name: "custom_pipeline" # Override pipeline name (default: source name)
  dataset_name: "raw_custom"       # Override dataset name (default: raw_<source_name>)
```

### Source Module Resolution

Dango looks for source modules in this order:

1. **`custom_sources/` directory** - Your custom sources
   - `source_module: "my_api"` → `custom_sources/my_api.py`

2. **Installed dlt packages** - dlt verified sources
   - `source_module: "zendesk"` → `dlt.sources.zendesk` (if installed)

3. **Installed Python packages** - Any importable module
   - `source_module: "my_company.sources.erp"` → Standard Python import

---

## Advanced Patterns

### 1. Passing Secrets Safely

**Use `dlt.secrets.value` decorator:**

```python
@dlt.source
def my_source(
    api_key: str = dlt.secrets.value,
    api_secret: str = dlt.secrets.value
):
    # Credentials loaded from .dlt/secrets.toml automatically
    pass
```

**Configure:**

```yaml
dlt_native:
  source_module: my_source
  source_function: my_source
  function_kwargs: {}  # Empty - secrets loaded automatically
```

**Secrets:**

```toml
[sources.my_source]
api_key = "xxxxx"
api_secret = "yyyyy"
```

### 2. Overriding Defaults

**Pass explicit values:**

```yaml
dlt_native:
  source_module: my_source
  source_function: my_source
  function_kwargs:
    api_key: "hardcoded_key"  # Override secret
    batch_size: 500            # Custom parameter
    start_date: "2024-01-01"   # Override default
```

### 3. Environment Variables

**Use `env:` prefix:**

```yaml
function_kwargs:
  api_key: "env:MY_API_KEY"
  endpoint: "env:API_ENDPOINT"
```

**In `.env`:**

```bash
MY_API_KEY=xxxxx
API_ENDPOINT=https://api.example.com
```

### 4. Nested Configuration

```yaml
function_kwargs:
  credentials:
    username: "env:DB_USER"
    password: "env:DB_PASS"
    host: "localhost"
  options:
    timeout: 30
    retries: 3
```

---

## Best Practices

### ✅ DO

1. **Version your custom sources**
   ```bash
   git add custom_sources/
   git commit -m "Add Lazada API source"
   ```

2. **Document your sources**
   ```python
   @dlt.source
   def my_source():
       """
       Load data from My API

       Authentication: API Key (get from https://myapi.com/settings)
       Rate Limits: 100 requests/minute
       Data Retention: 90 days

       Returns:
           - users: User accounts
           - orders: Order transactions
       """
       pass
   ```

3. **Test independently**
   ```python
   # test_lazada.py
   from custom_sources.lazada_api import lazada_source

   source = lazada_source(
       app_key="test",
       app_secret="test",
       access_token="test"
   )

   # Test source function
   for record in source.orders:
       print(record)
   ```

4. **Handle errors gracefully**
   ```python
   @dlt.resource
   def robust_api():
       try:
           response = requests.get(url)
           response.raise_for_status()
           yield response.json()
       except requests.HTTPError as e:
           if e.response.status_code == 429:
               time.sleep(60)  # Rate limit
           raise
   ```

### ❌ DON'T

1. **Don't hardcode secrets**
   ```python
   # BAD
   api_key = "sk_live_xxxxx"

   # GOOD
   api_key: str = dlt.secrets.value
   ```

2. **Don't skip error handling**
   ```python
   # BAD
   data = requests.get(url).json()

   # GOOD
   response = requests.get(url)
   response.raise_for_status()
   data = response.json()
   ```

3. **Don't ignore incremental loading**
   ```python
   # BAD - Always loads all data
   def orders():
       return api.get_all_orders()

   # GOOD - Incremental loading
   def orders(updated_at: dlt.sources.incremental("updated_at") = None):
       since = updated_at.last_value or "2020-01-01"
       return api.get_orders(since=since)
   ```

---

## Troubleshooting

### Import Error: Module not found

```
ModuleNotFoundError: No module named 'my_source'
```

**Solution:**
1. Check file exists: `custom_sources/my_source.py`
2. Check source_module matches filename (without .py)
3. Restart Dango if file was just created

### Function Not Found

```
ValueError: Function 'my_function' not found in module 'my_source'
```

**Solution:**
1. Check function name in code matches `source_function`
2. Ensure function is decorated with `@dlt.source`
3. Check for typos

### Credentials Not Loading

```
KeyError: 'api_key'
```

**Solution:**
1. Check `.dlt/secrets.toml` has correct section: `[sources.my_source]`
2. Use `dlt.secrets.value` decorator
3. Verify secret names match function parameters

### State Corruption

```
PipelineStateEngineError: State is corrupted
```

**Solution:**
```bash
dango sync --source my_source --full-refresh
```

---

## Migration Path

Want to add your `dlt_native` source to Dango's registry?

1. **Test your source** with real data
2. **Document thoroughly** (setup steps, credentials, resources)
3. **Create pull request** to Dango repo
4. **Add to registry** with wizard support

See `CONTRIBUTING.md` for details.

---

## Examples Library

### Stripe (Alternative Implementation)

```yaml
sources:
  - name: stripe_custom
    type: dlt_native
    dlt_native:
      source_module: stripe
      source_function: stripe_source
      function_kwargs:
        endpoint: "Event"
        start_date: "2024-01-01"
```

### MongoDB

```yaml
sources:
  - name: mongo_prod
    type: dlt_native
    dlt_native:
      source_module: mongodb
      source_function: mongodb
      function_kwargs:
        connection_url: "env:MONGODB_URL"
        database: "production"
        collections:
          - "users"
          - "orders"
          - "products"
```

### Kafka

```yaml
sources:
  - name: kafka_events
    type: dlt_native
    dlt_native:
      source_module: kafka
      source_function: kafka_consumer
      function_kwargs:
        bootstrap_servers: "localhost:9092"
        topics:
          - "user.events"
          - "order.events"
```

---

## Additional Resources

- **dlt Documentation**: https://dlthub.com/docs
- **Build a Pipeline Tutorial**: https://dlthub.com/docs/build-a-pipeline-tutorial
- **Verified Sources List**: https://dlthub.com/docs/dlt-ecosystem/verified-sources
- **Advanced Usage Guide**: docs/ADVANCED_USAGE.md
- **Custom Sources README**: custom_sources/README.md

---

*Last updated: Nov 2024*
