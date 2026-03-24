# Known Limitations

## GA4 & Matomo: Duplicate Row Risk

The vendored Google Analytics and Matomo dlt sources use **append mode** with no built-in deduplication. Re-syncing the same date range produces duplicate rows in DuckDB.

**Workaround:** Use full refresh (`dango db clean --source <name>` then re-sync) if you suspect duplicates. Future phases may add dedup at the ingestion layer.

## Facebook Ads: 60-Day Token Expiry

Facebook long-lived tokens expire after 60 days. Unlike Google OAuth, there is no automatic refresh.

**Workaround:** Run `dango oauth refresh <credential_name>` before the token expires. Dango warns at the 7-day mark via `dango status` and `dango oauth check`.

## Wizard-Only Testing Caveat

Most sources were tested through the wizard flow (parameter collection, config generation) but not with real API syncs in Phase 5. Sources marked "wizard flow verified; real sync not tested" have validated configuration but may encounter runtime issues with specific account configurations.

Sources with confirmed real-data syncs (Phase 5 P5-007 testing):
- REST API (JSONPlaceholder)
- Google Analytics (GA4)
- Facebook Ads
- Zendesk
- Slack
- Chess.com
- Workable

## Salesforce: Credential Restructuring

Salesforce credentials use a nested structure that differs from most sources. The `_CREDENTIAL_RESTRUCTURE` map in `dlt_runner.py` transforms flat environment variables into the nested credential objects Salesforce's dlt source expects.

## REST API: Transform Needed for dlt Format

The wizard collects endpoints as `{"path": "/users", "name": "users"}`, but the dlt `rest_api` source expects `{"name": "users", "endpoint": {"path": "/users"}}`. The runner handles this transformation automatically via `_build_rest_api_config()`.

## dlt Credential Resolution Varies by Source

Some dlt sources accept credentials as function parameters (e.g., `github_reactions(access_token=...)`), while others use `dlt.secrets.value` decorators on resource functions. Passing credential kwargs to the latter causes crashes. Dango's runner handles this routing automatically.

## Incremental Loading Caveats

- Sources marked "Incremental: No" perform full refreshes on every sync.
- Incremental sources track cursor state in dlt's pipeline state. Deleting the `.dlt/` directory resets this state.
- The `start_date` parameter (where available) only affects the first sync — subsequent syncs use the incremental cursor.
