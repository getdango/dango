# Dango Development - Start Here

**Last Updated:** November 26, 2025

This document provides context for continuing development on Dango.

---

## Current Status

### What's Working
- **Google Sheets OAuth**: Full end-to-end flow tested and working
  - OAuth browser-based authentication
  - Multi-sheet selection wizard
  - Data sync to DuckDB
  - Staging model auto-generation
  - Intermediate/Marts model creation
  - Metabase visualization (raw schemas hidden)

### Code Fixes Applied (Ready for Testing)
The following providers have had code fixes applied but NOT yet tested:
- **Shopify**: Fixed URL construction, removed debug print, added resource filtering
- **Google Analytics**: Fixed auth_type, removed duplicate credential prompts
- **Google Ads**: Removed duplicate credential prompts
- **Facebook Ads**: Fixed account_id prefix handling

### Next Steps
1. Test Shopify OAuth flow (create trial account, test full flow)
2. Test Google Ads/Analytics OAuth flow
3. Test Facebook Ads OAuth flow
4. Test edge cases (missing credentials, multiple sources)
5. **NEW**: Test advanced user features (dlt_native, custom macros, custom schemas)

---

## Key Files Modified Recently

### OAuth & Source Wizard
- `dango/oauth/providers.py` - OAuth provider implementations
- `dango/cli/source_wizard.py` - Source configuration wizard
- `dango/ingestion/sources/registry.py` - Source metadata registry

### dlt Source Fixes
- `dango/ingestion/dlt_sources/shopify_dlt/helpers.py` - Fixed URL, removed print
- `dango/ingestion/dlt_sources/shopify_dlt/__init__.py` - Added resources param

### Metabase Integration
- `dango/visualization/metabase.py` - Schema sync and visibility
- `dango/cli/main.py` - Added sync_metabase_schema after dbt run

### Model Wizard
- `dango/cli/model_wizard.py` - Success message restoration

---

## Architecture Overview

### Data Flow
```
dango source add → sources.yml
dango auth <provider> → .dlt/secrets.toml
dango sync → dlt pipeline → DuckDB (raw_* schemas)
dango model add → dbt models (staging/intermediate/marts)
dango run → dbt run → Metabase sync
```

### Key Components
- **Source Registry** (`registry.py`): Metadata for all 33 supported sources
- **OAuth Storage** (`oauth/storage.py`): Manages credentials in `.dlt/secrets.toml`
- **dlt Runner** (`dlt_runner.py`): Executes dlt pipelines for each source
- **Model Wizard** (`model_wizard.py`): Creates dbt models from sources

---

## Testing Documentation

See `docs/TESTING_PLAN.md` for:
- Detailed test procedures for each provider
- Progress tracking (what's tested, what's pending)
- Bugs found and fixes applied
- Expected behaviors and error handling

---

## Known Issues & Learnings

### Issue Categories Found During Testing

1. **Registry Misconfigurations**: Wrong `auth_type`, duplicate credential params
   - These prevent OAuth from triggering at all
   - Fix: Review registry entry matches working patterns (Google Sheets)

2. **dlt Source Bugs**: Debug prints, missing error handling, URL issues
   - These cause sync failures
   - Fix: Pre-review dlt source code before testing

3. **Metabase Integration**: Missing sync calls, silent failures
   - New schemas don't appear until sync triggered
   - Fix: Always call `sync_metabase_schema()` after dbt run

4. **Parameter Handling**: Inconsistent naming, prefix handling
   - Credentials stored with different formats than expected
   - Fix: Standardize on clean IDs, add prefixes in API calls

### Lesson Learned
**Code review before testing saves significant time.** Google Sheets took a full day due to debug-fix-retry cycles. By reviewing Shopify/GA/GAds/FB code upfront, we found 10+ issues that would have caused testing failures.

---

## dbt Advanced Features - Fully Supported ✅

Dango places **no artificial limitations** on dbt. Advanced users can:

| Feature | Status | How |
|---------|--------|-----|
| **Custom macros** | ✅ Full | Add `.sql` files to `dbt/macros/` |
| **Custom schemas** | ✅ Full | Add config to `dbt_project.yml` |
| **All materializations** | ✅ Full | table, view, incremental, ephemeral |
| **Tests** | ✅ Full | Built-in + custom tests |
| **Snapshots (SCD)** | ✅ Full | `dbt/snapshots/` directory |
| **Seeds** | ✅ Full | CSV loading via `dbt seed` |
| **Packages** | ✅ Full | `packages.yml` + `dbt deps` |
| **Direct dbt CLI** | ✅ Full | `cd dbt && dbt <command>` |

**Key design**: Auto-generated models have a marker comment. Remove it to "claim" the model - Dango won't overwrite customized models.

**Metabase visibility for custom schemas**: Custom schemas (beyond staging/intermediate/marts) are **visible by default** in Metabase. The current logic:
- `raw_*` and `*_staging` schemas → Hidden
- `staging`, `intermediate`, `marts` → Visible with descriptions
- Custom schemas → Visible (no description set)

Users cannot configure visibility via Dango yet, but can manually adjust in Metabase Admin → Data Model.

---

## dlt Integration Analysis

### Are Issues Wizard-Specific or dlt Integration Problems?

**Summary**: Most issues are **Dango wizard/registry misconfigurations**, NOT fundamental dlt integration problems.

#### Issues by Category:

**Wizard/Registry Issues (Dango-specific):**
- Wrong `auth_type` in registry → OAuth flow never triggers
- Duplicate credential params → User asked twice for same info
- Parameter naming inconsistencies → Credentials not found
- Missing Metabase sync call → New schemas don't appear

**dlt Source Issues (Would affect any dlt user):**
- Debug `print()` in Shopify source → Affects all users
- Empty sheet handling in Google Sheets → Affects all users
- URL construction without protocol → Affects all users

#### Confidence in dlt_native/Bypass Mode

**Medium-High Confidence**: Users who configure dlt sources directly via `dlt_native` should work correctly because:

1. **dlt sources themselves are stable** - The core dlt pipeline execution works
2. **Credential loading works** - `.dlt/secrets.toml` format is standard dlt
3. **Most bugs are wizard-specific** - Manual config bypasses wizard issues

**However**, some dlt sources have bugs that affect ALL users:
- Shopify debug print (fixed)
- Empty sheet handling (fixed via pre-filtering)

**Recommendation**: For MVP, the wizard-supported sources are safest. `dlt_native` is an escape hatch for advanced users who understand dlt.

---

## Files to Review Before Continuing

1. **TESTING_PLAN.md** - Full test progress and remaining work
2. **OAUTH_SETUP.md** - User-facing OAuth setup documentation
3. **REGISTRY_BYPASS.md** - How `dlt_native` works for advanced users
4. **Plan file** - `/Users/aaronteoh/.claude/plans/vectorized-frolicking-catmull.md`

---

## Quick Commands

```bash
# Install dango (development)
pip install -e /Users/aaronteoh/Desktop/code/getdango/dango

# Test OAuth flow
dango auth shopify

# Add source
dango source add

# Sync data
dango sync

# Create model
dango model add

# Run dbt
dango run
```
