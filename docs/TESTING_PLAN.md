# OAuth Implementation Testing Plan

**Version:** v0.1.0
**Last Updated:** November 26, 2025
**Status:** Google Sheets COMPLETE, code fixes done for remaining providers, testing in progress

---

## TESTING PROGRESS (as of Nov 26, 2025)

### Phase 1: Google Sheets OAuth - COMPLETED ✅

**All tests passed:**
- [x] Google Cloud Project created
- [x] APIs enabled (Sheets + Drive)
- [x] OAuth credentials created (Web app, redirect URI)
- [x] Test spreadsheet created with data (multi-sheet)
- [x] `dango auth google_sheets` completed
- [x] Browser opened automatically
- [x] OAuth consent granted
- [x] Refresh token received
- [x] Credentials saved to .dlt/secrets.toml
- [x] Google Sheets source added (multi-sheet selection working)
- [x] Sync completed successfully
- [x] Staging models auto-generated
- [x] Intermediate model created with `dango model add`
- [x] Marts model created with `dango model add`
- [x] `dango run` executed successfully
- [x] Metabase shows correct schemas (intermediate, marts, staging only - raw hidden)

**Bugs Found & Fixed:**

| Issue | Root Cause | Fix Applied |
|-------|------------|-------------|
| Marts schema not showing in Metabase | `dango run` only called `refresh_metabase_connection()` not `sync_metabase_schema()` | Added `sync_metabase_schema()` in `main.py:2992-3002` |
| Raw schemas still visible in Metabase | PUT requests for visibility had no error handling | Added logging and status checking in `metabase.py:973-987` |
| Model wizard success message oversimplified | Previous change removed too much content | Restored full message in `model_wizard.py:115-159` |
| Empty sheets cause sync failure | dlt Google Sheets source has fragile error handling | Added empty sheet pre-filtering in `source_wizard.py:1065-1103` |

**Remaining verification (user will test later):**
- [ ] Empty sheet filtering verification
- [ ] Model wizard success message review

---

### Code Review: Issues Found & Fixed (Nov 26, 2025)

Before testing remaining providers, we conducted a code review to identify potential issues upfront. This avoids the debug-fix-retry cycle that made Google Sheets testing take a full day.

#### Shopify Issues - FIXED ✅

| # | Issue | Severity | File | Status |
|---|-------|----------|------|--------|
| S1 | Debug `print(data)` in production | HIGH | `shopify_dlt/helpers.py:138` | FIXED - Removed |
| S2 | Invalid URL construction (missing https://) | HIGH | `shopify_dlt/helpers.py:50` | FIXED - Added protocol normalization |
| S3 | Resources parameter not passed to source | HIGH | `shopify_dlt/__init__.py` | FIXED - Added resources param |

#### Google Analytics Issues - FIXED ✅

| # | Issue | Severity | File | Status |
|---|-------|----------|------|--------|
| GA1 | Wrong `auth_type` (SERVICE_ACCOUNT instead of OAUTH) | CRITICAL | `registry.py:267` | FIXED - Changed to OAUTH |
| GA2 | Asks for `credentials_env` (duplicates OAuth) | CRITICAL | `registry.py:278-283` | FIXED - Removed |
| GA3 | `property_id` prompted in OAuth (should be source param) | HIGH | `providers.py:232-237` | FIXED - Removed from OAuth |

#### Google Ads Issues - FIXED ✅

| # | Issue | Severity | File | Status |
|---|-------|----------|------|--------|
| GAD1 | Asks for `credentials_env` (duplicates OAuth) | CRITICAL | `registry.py:644-650` | FIXED - Removed |
| GAD2 | `impersonated_email` param (service account only) | HIGH | `registry.py:658-663` | FIXED - Removed |

#### Facebook Ads Issues - FIXED ✅

| # | Issue | Severity | File | Status |
|---|-------|----------|------|--------|
| FB1 | Account ID prefix inconsistency ("act_" handling) | HIGH | `providers.py:446` | FIXED - Store clean ID |

---

### Phase 2: Shopify OAuth - NOT STARTED

**Ready for testing.** Code fixes applied.

### Phase 3: Google Ads/Analytics OAuth - NOT STARTED

**Ready for testing.** Code fixes applied.

### Phase 4: Facebook Ads OAuth - NOT STARTED

**Ready for testing.** Code fixes applied.

### Phase 5: Edge Cases - NOT STARTED

### Phase 6: Advanced User Validation - NOT STARTED (NEW)

**Purpose**: Validate that advanced users (dlt/dbt experts) can use Dango as a platform without the wizard.

---

## NEW: Phase 6 - Advanced User Validation Tests

### Test 6.1: dlt_native Bypass Mode

**Purpose**: Verify that users can configure any dlt source manually without using the wizard.

**Steps:**
```bash
# 1. Create fresh project
dango init advanced-test
cd advanced-test

# 2. Manually add a non-wizard source to sources.yml
cat >> .dango/sources.yml << 'EOF'
  - name: github_test
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: "github"
      source_function: "github_reactions"
      function_kwargs:
        owner: "dlt-hub"
        name: "dlt"
EOF

# 3. Add credentials to .dlt/secrets.toml (standard dlt format)
cat >> .dlt/secrets.toml << 'EOF'
[sources.github]
access_token = "ghp_your_token_here"
EOF

# 4. Run sync
dango sync
```

**Expected:**
- [ ] dlt pipeline executes successfully
- [ ] Data appears in DuckDB under `raw_github_test` schema
- [ ] No wizard-related errors
- [ ] Staging models can be generated with `dango model add`

---

### Test 6.2: Custom dbt Macros

**Purpose**: Verify users can add custom macros and use them in models.

**Steps:**
```bash
# 1. Create custom macro
cat > dbt/macros/custom_helpers.sql << 'EOF'
{% macro cents_to_dollars(column_name) %}
    ({{ column_name }} / 100.0)
{% endmacro %}

{% macro is_valid_email(column_name) %}
    ({{ column_name }} LIKE '%@%.%')
{% endmacro %}
EOF

# 2. Create model using custom macro
cat > dbt/models/marts/test_custom_macro.sql << 'EOF'
{{ config(schema='marts') }}

SELECT
    1 as id,
    100 as amount_cents,
    {{ cents_to_dollars('amount_cents') }} as amount_dollars
EOF

# 3. Run dbt
dango run --select test_custom_macro
```

**Expected:**
- [ ] Macro compiles without errors
- [ ] Model runs successfully
- [ ] `amount_dollars` column shows 1.0 (not 100)

---

### Test 6.3: Custom Schemas

**Purpose**: Verify users can create schemas beyond staging/intermediate/marts.

**Steps:**
```bash
# 1. Add custom schema config to dbt_project.yml
# Add under models section:
#   reporting:
#     +materialized: table
#     +schema: reporting

# 2. Create reporting directory and model
mkdir -p dbt/models/reporting
cat > dbt/models/reporting/weekly_summary.sql << 'EOF'
{{ config(schema='reporting') }}

SELECT
    'test' as metric_name,
    100 as value
EOF

# 3. Run dbt
dango run --select weekly_summary

# 4. Check schema was created
# In DuckDB: SHOW SCHEMAS; should include 'reporting'
```

**Expected:**
- [ ] `reporting` schema created in DuckDB
- [ ] Model appears in `reporting` schema
- [ ] Metabase discovers new schema after sync

---

### Test 6.4: dbt Packages

**Purpose**: Verify users can install and use dbt packages.

**Steps:**
```bash
# 1. Create packages.yml
cat > dbt/packages.yml << 'EOF'
packages:
  - package: dbt-labs/dbt_utils
    version: 1.1.1
EOF

# 2. Install packages
cd dbt && dbt deps && cd ..

# 3. Use package macro in a model
cat > dbt/models/marts/test_dbt_utils.sql << 'EOF'
{{ config(schema='marts') }}

SELECT
    {{ dbt_utils.generate_surrogate_key(['id', 'name']) }} as surrogate_key,
    1 as id,
    'test' as name
EOF

# 4. Run model
dango run --select test_dbt_utils
```

**Expected:**
- [ ] `dbt deps` installs packages successfully
- [ ] Model compiles and runs with package macro
- [ ] Surrogate key is generated correctly

---

### Test 6.5: Direct dbt CLI Access

**Purpose**: Verify users can run dbt commands directly without Dango wrapper.

**Steps:**
```bash
cd dbt

# Test various dbt commands
dbt debug          # Should show connection working
dbt parse          # Should parse all models
dbt compile        # Should compile models
dbt test           # Should run tests (if any)
dbt docs generate  # Should generate docs
dbt docs serve     # Should start docs server (manual check)
```

**Expected:**
- [ ] All dbt commands work without errors
- [ ] No Dango-specific interference
- [ ] Users can work in standard dbt workflow

---

## Overview

This document provides a comprehensive testing plan for the OAuth implementation in Dango. The OAuth feature enables browser-based authentication for Google Ads, Google Analytics, Google Sheets, Facebook/Meta Ads, and Shopify.

## Testing Timeline

**Total Estimated Time:** 6-8 hours
**Recommended Schedule:** 1 full day of focused testing

---

## Phase 1: Google Sheets OAuth Testing (PRIORITY 1)

**Duration:** 2 hours
**Why First:** Simplest OAuth flow, free tier available, validates core infrastructure

### Prerequisites Setup (30 minutes)

**Step 1: Create Google Cloud Project**
1. Go to https://console.cloud.google.com/
2. Click "Select a project" → "New Project"
3. Name: `dango-oauth-test`
4. Click "Create"
5. Wait for project creation (notification appears)

**Step 2: Enable Required APIs**
1. In the Google Cloud Console, go to "APIs & Services" → "Library"
2. Search for "Google Sheets API" → Click → "Enable"
3. Search for "Google Drive API" → Click → "Enable"
4. Wait for both APIs to enable (takes ~30 seconds each)

**Step 3: Create OAuth 2.0 Credentials**
1. Go to "APIs & Services" → "Credentials"
2. Click "+ CREATE CREDENTIALS" → "OAuth client ID"
3. If prompted, configure OAuth consent screen:
   - User Type: **External**
   - App name: `Dango OAuth Test`
   - User support email: your email
   - Developer contact: your email
   - Save and continue through remaining screens (Scopes: skip, Test users: add your email, Summary: save)
4. Return to "Create OAuth client ID":
   - Application type: **Web application** (CRITICAL - not Desktop app!)
   - Name: `Dango Local Testing`
   - Authorized redirect URIs: Add `http://localhost:8080/callback`
   - Click "Create"
5. **SAVE** the Client ID and Client Secret (shown in popup)
   - Copy to a text file for easy access during testing

**Step 4: Create Test Spreadsheet**
1. Go to https://sheets.google.com/
2. Create a new spreadsheet
3. Add some test data:
   ```
   | order_id | product    | amount | date       |
   |----------|------------|--------|------------|
   | 1        | Widget A   | 100    | 2024-01-01 |
   | 2        | Widget B   | 150    | 2024-01-02 |
   | 3        | Widget C   | 200    | 2024-01-03 |
   ```
4. Note the Spreadsheet ID from URL:
   - URL: `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`
   - Copy the `SPREADSHEET_ID` part

### Environment Setup (15 minutes)

**Step 1: Install Dango from Feature Branch**
```bash
# Create clean test directory
cd ~/Desktop
mkdir dango-oauth-testing
cd dango-oauth-testing

# Install from GitHub branch
pip install git+https://github.com/getdango/dango.git@feature/oauth-implementation

# Verify installation
dango --version  # Should show development version
```

**Step 2: Initialize Test Project**
```bash
# Create new Dango project
dango init oauth-test-project
cd oauth-test-project

# Verify directory structure
ls -la
# Should see: .dango/, .dlt/, data/, dbt/, .gitignore, etc.

# Verify .dlt/ directory created
ls -la .dlt/
# Should see: secrets.toml, config.toml

# Check .gitignore includes .dlt/secrets.toml
cat .gitignore | grep .dlt
# Should see: .dlt/secrets.toml
```

### OAuth Flow Testing (45 minutes)

**Test 1: Run OAuth Authentication**
```bash
dango auth google_sheets
```

**Expected Behavior:**
1. ✅ Displays setup instructions with Google Cloud Console link
2. ✅ Prompts: "Open Google Cloud Console? [Y/n]" → Press Y
3. ✅ Browser opens to Google Cloud Console (optional)
4. ✅ Prompts: "Enter OAuth Client ID:" → Paste your Client ID
5. ✅ Prompts: "Enter Client Secret:" → Paste your Client Secret
6. ✅ Shows: "Starting local callback server..."
7. ✅ Shows: "Callback server listening on port 8080"
8. ✅ Shows: "Opening browser for authorization..."
9. ✅ Browser automatically opens Google sign-in page
10. ✅ After sign-in, shows permission screen:
    - "Dango OAuth Test wants to access your Google Account"
    - Permissions: "See, edit, create, and delete all your Google Sheets spreadsheets"
    - Permissions: "See and download all your Google Drive files"
11. ✅ Click "Allow"
12. ✅ Browser shows: "✓ Authentication Successful! You can close this window"
13. ✅ Terminal shows: "✓ Authorization received!"
14. ✅ Terminal shows: "Exchanging authorization code for tokens..."
15. ✅ Terminal shows: "✓ Tokens received successfully!"
16. ✅ Terminal shows: "✓ Credentials saved to .dlt/secrets.toml"
17. ✅ Terminal shows success message with next steps

**If "No refresh token received" warning appears:**
- This means you've authorized this app before
- Go to https://myaccount.google.com/permissions
- Find "Dango OAuth Test" → Remove access
- Re-run `dango auth google_sheets`

**Test 2: Verify Credentials Saved**
```bash
cat .dlt/secrets.toml
```

**Expected Content:**
```toml
[sources.google_sheets]
client_id = "123456789.apps.googleusercontent.com"
client_secret = "GOCSPX-xxxxxxxxxxxxx"
refresh_token = "1//xxxxxxxxxxxxx"
project_id = ""
```

✅ **Verify:**
- File exists
- Has `[sources.google_sheets]` section
- Contains `client_id`, `client_secret`, `refresh_token`
- Values are not empty strings

**Test 3: Add Google Sheets Source**
```bash
dango source add
```

**Expected Flow:**
1. ✅ Category selection appears
2. ✅ Select "Marketing & Analytics"
3. ✅ Source list shows "Google Sheets"
4. ✅ Select "Google Sheets"
5. ✅ Prompts for source name → Enter: `test_sheets`
6. ✅ Prompts for Spreadsheet ID → Paste your SPREADSHEET_ID
7. ✅ Prompts for sheet name (optional) → Press Enter (all sheets)
8. ✅ **SHOULD NOT** prompt for OAuth credentials (already authenticated)
9. ✅ Shows: "✓ Source 'test_sheets' added successfully"

**Test 4: Verify Source Configuration**
```bash
cat .dango/sources.yml
```

**Expected Content:**
```yaml
sources:
  - name: test_sheets
    type: google_sheets
    enabled: true
    google_sheets:
      spreadsheet_id: "YOUR_SPREADSHEET_ID"
      sheet_name: null
      credentials_env: "GOOGLE_CREDENTIALS"
```

**Test 5: Run Data Sync**
```bash
dango sync
```

**Expected Behavior:**
1. ✅ Shows: "Syncing: test_sheets (google_sheets)"
2. ✅ Changes directory to project root (for .dlt/ loading)
3. ✅ dlt creates pipeline
4. ✅ dlt loads credentials from `.dlt/secrets.toml`
5. ✅ Shows progress: "Loading data..."
6. ✅ Shows: "✓ Loaded X rows" (should match your spreadsheet)
7. ✅ No authentication errors
8. ✅ Sync completes successfully

**Test 6: Verify Data Loaded**
```bash
# Check DuckDB has data
sqlite3 data/warehouse.duckdb "SELECT * FROM raw.test_sheets LIMIT 5;"

# Or use dbt
cd dbt
dbt run
```

✅ **Verify:**
- Data appears in DuckDB
- Row count matches spreadsheet
- Column names match

---

## Phase 2: Shopify OAuth Testing (PRIORITY 2)

**Duration:** 1.5 hours
**Why Second:** Simple setup, free trial, different auth pattern

### Prerequisites Setup (45 minutes)

**Step 1: Create Shopify Trial Account**
1. Go to https://www.shopify.com/free-trial
2. Click "Start free trial"
3. Enter email → Click "Start free trial"
4. Store name: `dango-test-store` (or any name)
5. Complete signup (no credit card required for 3-day trial)
6. Skip product/payment setup steps
7. Go to Shopify Admin dashboard

**Step 2: Create Custom App**
1. In Shopify Admin, go to: **Settings** (bottom left)
2. Click **Apps and sales channels**
3. Click **Develop apps** (or "Develop apps for your store")
4. Click **Create an app**
5. App name: `Dango Data Sync`
6. App developer: Your name/email
7. Click **Create app**

**Step 3: Configure Admin API Access**
1. In the app page, click **Configure Admin API scopes**
2. Find and enable these scopes (minimum):
   - `read_orders` - Read orders
   - `read_customers` - Read customers
   - `read_products` - Read products
3. Click **Save**
4. Click **Install app** (top right)
5. Click **Install** to confirm

**Step 4: Get Access Token**
1. After installation, click **API credentials** tab
2. Under "Admin API access token", click **Reveal token once**
3. **COPY THE TOKEN** immediately (shown only once!)
   - Format: `shpat_xxxxxxxxxxxxx`
4. Save it in a text file for testing
5. Note your store URL: `dango-test-store.myshopify.com`

### OAuth Flow Testing (45 minutes)

**Test 1: Run Shopify Authentication**
```bash
cd ~/Desktop/dango-oauth-testing/oauth-test-project
dango auth shopify
```

**Expected Behavior:**
1. ✅ Displays setup instructions for creating custom app
2. ✅ Prompts: "Open Shopify admin panel? [Y/n]" → Optional
3. ✅ Prompts: "Shop URL (e.g., mystore.myshopify.com):" → Enter your store URL
4. ✅ If you enter without `.myshopify.com`, it auto-appends
5. ✅ Prompts: "Admin API access token:" → Paste token (hidden as password)
6. ✅ Shows: "Testing connection..."
7. ✅ Shows: "✓ Connected to shop: Dango Test Store" (or your shop name)
8. ✅ Shows: "✓ Credentials saved to .dlt/secrets.toml"
9. ✅ Shows success message

**Test 2: Verify Credentials**
```bash
cat .dlt/secrets.toml
```

**Should now have:**
```toml
[sources.google_sheets]
# ... (from previous test)

[sources.shopify]
private_app_password = "shpat_xxxxxxxxxxxxx"
shop_url = "dango-test-store.myshopify.com"
```

**Test 3: Add Shopify Source**
```bash
dango source add
```

**Expected Flow:**
1. ✅ Select "E-commerce & Payment" category
2. ✅ Select "Shopify"
3. ✅ Source name → `shopify_orders`
4. ✅ Prompts for Shop URL → Should auto-fill from credentials
5. ✅ Prompts for resources to sync → Select "orders", "customers", "products"
6. ✅ **SHOULD NOT** prompt for access token (already authenticated)
7. ✅ Source added successfully

**Test 4: Sync Shopify Data**
```bash
dango sync
```

✅ **Verify:**
- Sync completes without auth errors
- Data loaded (even if 0 rows for new store)
- No connection failures

---

## Phase 3: Edge Cases & Error Handling (PRIORITY 3)

**Duration:** 1-2 hours

### Test 1: Missing OAuth Credentials

**Scenario:** Add OAuth source without running `dango auth` first

```bash
# Create fresh project
cd ~/Desktop/dango-oauth-testing
dango init test-no-auth
cd test-no-auth

# Try to add Google Sheets WITHOUT auth
dango source add
# Select Google Sheets, enter spreadsheet ID

# Then try to sync
dango sync
```

**Expected Behavior:**
- ✅ Sync fails with clear error: "Missing OAuth credentials for google_sheets"
- ✅ Error message suggests: "Run: dango auth google_sheets"
- ✅ Does NOT show cryptic dlt errors

### Test 2: Invalid/Expired Credentials

**Scenario:** Manually corrupt credentials to simulate expiry

```bash
# Edit .dlt/secrets.toml
nano .dlt/secrets.toml

# Change refresh_token to: "invalid_token_12345"
# Save and exit

# Try to sync
dango sync
```

**Expected Behavior:**
- ✅ Sync fails with authentication error
- ✅ Error message is clear
- ✅ Suggests re-running auth command

### Test 3: Multiple Sources, Same OAuth

**Scenario:** Add two Google Sheets sources

```bash
# Already have test_sheets from Phase 1
# Add another sheet
dango source add
# Select Google Sheets
# Name: test_sheets_2
# Different spreadsheet ID

# Sync both
dango sync
```

**Expected Behavior:**
- ✅ Both sources use same OAuth credentials
- ✅ No duplicate authentication required
- ✅ Both sync successfully

### Test 4: .env Fallback (Backward Compatibility)

**Scenario:** Test that old .env format still works

```bash
# Remove .dlt/ directory
rm -rf .dlt/

# Create .env file with old format
cat > .env <<EOF
GOOGLE_CREDENTIALS={"client_id":"xxx","client_secret":"xxx","refresh_token":"xxx"}
EOF

# Try to sync
dango sync
```

**Expected Behavior:**
- ✅ System falls back to .env
- ✅ Warning shown: "Using .env for credentials. Consider migrating to .dlt/"
- ✅ Sync works

### Test 5: Credential Priority (.dlt/ > .env)

**Scenario:** Have credentials in both files

```bash
# Have both .dlt/secrets.toml and .env
# With DIFFERENT credentials

# Sync
dango sync
```

**Expected Behavior:**
- ✅ Uses .dlt/secrets.toml credentials (higher priority)
- ✅ .env credentials ignored
- ✅ No conflicts

### Test 6: Port 8080 Already in Use

**Scenario:** OAuth callback port conflict

```bash
# Start a server on port 8080
python3 -m http.server 8080 &

# Try to run auth
dango auth google_sheets
```

**Expected Behavior:**
- ✅ Clear error: "Port 8080 is already in use"
- ✅ Suggests: "Stop the conflicting process or change the port"
- ✅ Does NOT crash or hang

**Cleanup:**
```bash
# Kill the test server
pkill -f "http.server 8080"
```

---

## Phase 4: Cross-Platform Testing (PRIORITY 4)

### macOS Testing

**Already on Mac - should work**

**Verify:**
- ✅ Browser opens automatically (`webbrowser.open()`)
- ✅ Callback server starts on port 8080
- ✅ Timeout mechanism works (`signal.alarm`)
- ✅ All OAuth flows complete successfully

### Windows Testing (If Available)

**Setup:**
```powershell
# Install Dango on Windows
pip install git+https://github.com/getdango/dango.git@feature/oauth-implementation

# Initialize project
dango init windows-test
cd windows-test

# Run Google Sheets OAuth
dango auth google_sheets
```

**Expected Behavior:**
- ✅ Warning shown: "Timeout not supported on Windows"
- ✅ OAuth flow still works (no timeout enforcement)
- ✅ Browser opens correctly
- ✅ Callback server works
- ✅ Credentials saved properly

**Known Limitations:**
- No timeout on Windows (signal.alarm not available)
- User can take as long as needed (no 5-minute timeout)

---

## Phase 5: Complex OAuth Providers (OPTIONAL - May Skip for MVP)

### Google Ads Testing

**Why Complex:**
- Requires Developer Token (approval takes days/weeks)
- Requires billing setup
- Requires ad account with spend

**Setup Required:**
1. Google Cloud Project with Google Ads API enabled
2. OAuth credentials (same as Google Sheets)
3. Developer Token (apply at https://ads.google.com/aw/apicenter)
4. Customer ID from active ad account

**Testing:**
```bash
dango auth google_ads
# Provide OAuth credentials
# Provide Developer Token
# Provide Customer ID
```

**Expected:** Full OAuth flow + additional credentials

### Facebook Ads Testing

**Why Complex:**
- Requires Facebook Business Manager
- Requires Ad Account
- Complex app review process for production

**Setup Required:**
1. Facebook Developer account
2. Business App created
3. Marketing API added
4. Ad account linked

**Testing:**
```bash
dango auth facebook_ads
# Follow manual token flow
# Exchange for long-lived token
```

**Expected:** 60-day token obtained

### Google Analytics Testing

**Why Complex:**
- Requires website/app with GA4
- Or use Google demo account

**Setup:**
- Can use Google Analytics demo account
- Property ID: (from demo account)

**Testing:**
```bash
dango auth google_analytics
# OAuth flow
# Provide Property ID
```

---

## Test Results Tracking

### Google Sheets OAuth
- [ ] Prerequisites setup completed
- [ ] OAuth flow completed successfully
- [ ] Credentials saved to .dlt/secrets.toml
- [ ] Source added without credential prompts
- [ ] Data sync successful
- [ ] Data verified in DuckDB

### Shopify OAuth
- [ ] Trial account created
- [ ] Custom app configured
- [ ] OAuth flow completed
- [ ] Credentials saved
- [ ] Source added
- [ ] Sync successful

### Edge Cases
- [ ] Missing credentials error handled
- [ ] Invalid credentials error handled
- [ ] Multiple sources same OAuth works
- [ ] .env fallback works
- [ ] Credential priority correct (.dlt/ > .env)
- [ ] Port conflict handled

### Cross-Platform
- [ ] macOS works (current environment)
- [ ] Windows tested (if available)

### Optional Providers
- [ ] Google Ads (may skip - complex setup)
- [ ] Facebook Ads (may skip - complex setup)
- [ ] Google Analytics (may skip - needs GA4 property)

---

## Issues & Bugs Found

**Template for reporting issues:**

```markdown
### Issue: [Brief Description]
**Provider:** [google_sheets/shopify/etc]
**Severity:** [Critical/High/Medium/Low]
**Steps to Reproduce:**
1.
2.
3.

**Expected Behavior:**

**Actual Behavior:**

**Error Messages:**
```

**Screenshot/Logs:**

**Environment:**
- OS: [macOS/Windows]
- Python Version:
- Dango Version:
```

---

## Success Criteria

### Must Pass (Blocking for MVP):
- ✅ Google Sheets OAuth works end-to-end
- ✅ Credentials saved to .dlt/secrets.toml correctly
- ✅ dlt loads credentials successfully
- ✅ Data syncs without authentication errors
- ✅ .env fallback works (backward compatibility)
- ✅ Error messages are clear and helpful

### Should Pass (Important but not blocking):
- ✅ Shopify OAuth works
- ✅ Edge cases handled gracefully
- ✅ Port conflicts show clear errors
- ✅ Windows compatibility verified

### Nice to Have (Post-MVP):
- ✅ Google Ads OAuth tested with real account
- ✅ Facebook Ads OAuth tested
- ✅ Google Analytics OAuth tested

---

## Next Steps After Testing

1. **If all tests pass:**
   - Merge PR to main
   - Update CHANGELOG.md
   - Prepare for v0.1.0 release

2. **If issues found:**
   - Create GitHub issues for each bug
   - Prioritize by severity
   - Fix critical bugs before merging
   - Document known limitations

3. **Documentation:**
   - Update OAUTH_SETUP.md with any discoveries
   - Add troubleshooting for common issues found
   - Create video walkthrough (optional)

4. **Communication:**
   - Update team on test results
   - Share limitations discovered
   - Plan for complex provider support (Google Ads, Facebook Ads)

---

## Testing Checklist

Print this and check off as you test:

```
OAuth Implementation Testing Checklist

PHASE 1: GOOGLE SHEETS (2 hours)
[ ] Google Cloud Project created
[ ] APIs enabled (Sheets + Drive)
[ ] OAuth credentials created (Web app, redirect URI)
[ ] Test spreadsheet created with data
[ ] Dango installed from feature branch
[ ] Project initialized
[ ] .dlt/ directory verified
[ ] `dango auth google_sheets` completed
[ ] Browser opened automatically
[ ] OAuth consent granted
[ ] Refresh token received
[ ] Credentials saved to .dlt/secrets.toml
[ ] Google Sheets source added
[ ] No credential prompts (already authenticated)
[ ] Sync completed successfully
[ ] Data verified in DuckDB

PHASE 2: SHOPIFY (1.5 hours)
[ ] Shopify trial account created
[ ] Custom app created
[ ] API scopes configured
[ ] Access token revealed and saved
[ ] `dango auth shopify` completed
[ ] Connection test passed
[ ] Credentials saved to .dlt/secrets.toml
[ ] Shopify source added
[ ] Sync completed

PHASE 3: EDGE CASES (1-2 hours)
[ ] Missing credentials error tested
[ ] Invalid credentials error tested
[ ] Multiple sources same OAuth tested
[ ] .env fallback tested
[ ] Credential priority tested
[ ] Port conflict tested

PHASE 4: CROSS-PLATFORM
[ ] macOS tested
[ ] Windows tested (if available)

NOTES/ISSUES:
_______________________________________
_______________________________________
_______________________________________
```

---

**Happy Testing! 🍡**
