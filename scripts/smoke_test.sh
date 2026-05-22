#!/usr/bin/env bash
# scripts/smoke_test.sh — v1.7 (2026-05-22)
#
# Automated smoke test for a running Dango instance.
# Requires: dango start running in a test project, venv activated.
#
# Usage:
#   ./scripts/smoke_test.sh [BASE_URL]
#   ./scripts/smoke_test.sh --help
#
# Environment variables:
#   DANGO_BASE_URL — Server URL (default: http://localhost:8800)

# Note: -e is intentionally omitted — test commands are expected to return
# non-zero on failure; the script handles each result individually.
set -uo pipefail

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $0 [BASE_URL]"
    echo ""
    echo "Run automated smoke tests against a running Dango instance."
    echo ""
    echo "Arguments:"
    echo "  BASE_URL    Server URL (default: \$DANGO_BASE_URL or http://localhost:8800)"
    echo ""
    echo "Authentication: auto-creates a temporary API key from .dango/auth.db"
    exit 0
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL="${1:-${DANGO_BASE_URL:-http://localhost:8800}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Create a temporary API key for authentication (requires .dango/auth.db)
AUTH_RESULT=$(python3 -c "
from pathlib import Path; from dango.auth.admin import get_auth_db_path
from dango.auth.database import list_users; from dango.auth.sessions import create_api_key
from dango.auth.models import Role
db_path = get_auth_db_path(Path('.'))
users = list_users(db_path, active_only=True)
admin = next((u for u in users if u.role == Role.ADMIN), None)
if admin is None: raise SystemExit('No active admin user found in auth.db')
raw_key, api_key = create_api_key(db_path, admin.id, 'smoke_test_temp')
print(f'{raw_key}|{api_key.id}')
" 2>&1) || { echo "ERROR: Failed to create API key: $AUTH_RESULT"; exit 1; }
API_KEY="${AUTH_RESULT%%|*}"
API_KEY_ID="${AUTH_RESULT##*|}"

cleanup() {
    python3 -c "
from pathlib import Path
from dango.auth.admin import get_auth_db_path
from dango.auth.sessions import revoke_api_key
revoke_api_key(get_auth_db_path(Path('.')), '$API_KEY_ID')
" 2>/dev/null || true
}
trap cleanup EXIT

# Check BASE_URL reachability
if ! curl --head --silent --connect-timeout 5 "$BASE_URL" > /dev/null 2>&1; then
    echo "ERROR: Cannot reach $BASE_URL — is Dango running?"
    echo "       Start with: dango start"
    exit 1
fi

# ---------------------------------------------------------------------------
# Counters and state
# ---------------------------------------------------------------------------

TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0
CAT_PASS=0
CAT_FAIL=0
CAT_SKIP=0
CAT_TOTAL=0
START_TIME=$(date +%s)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pass_test() {
    CAT_PASS=$((CAT_PASS + 1))
    TOTAL_PASS=$((TOTAL_PASS + 1))
}

fail_test() {
    local name="$1"
    local detail="${2:-}"
    CAT_FAIL=$((CAT_FAIL + 1))
    TOTAL_FAIL=$((TOTAL_FAIL + 1))
    echo "    FAIL: $name"
    if [ -n "$detail" ]; then
        echo "          $detail"
    fi
}

skip_test() {
    local name="$1"
    local reason="${2:-}"
    CAT_SKIP=$((CAT_SKIP + 1))
    TOTAL_SKIP=$((TOTAL_SKIP + 1))
    echo "    SKIP: $name ($reason)"
}

category_start() {
    CAT_PASS=0
    CAT_FAIL=0
    CAT_SKIP=0
    CAT_TOTAL="$1"
}

category_end() {
    local num="$1"
    local name="$2"
    local status="PASS"
    local extra=""
    if [ "$CAT_FAIL" -gt 0 ]; then
        status="FAIL"
    fi
    if [ "$CAT_SKIP" -gt 0 ]; then
        extra=" ($CAT_SKIP SKIP)"
    fi
    printf "[%s/12] %-28s %d/%d %s%s\n" "$num" "$name" "$CAT_PASS" "$CAT_TOTAL" "$status" "$extra"
}

# Run a command, pass if exit code is 0
run_cmd_test() {
    local name="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        pass_test
    else
        fail_test "$name"
    fi
}

# Execute an authenticated API curl, print the HTTP status code.
# Supports GET (default), POST, PUT, DELETE methods.
_curl_api_status() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    local args=(-s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $API_KEY")
    args+=(-H "X-Requested-With: XMLHttpRequest")

    if [ "$method" = "POST" ]; then
        args+=(-X POST -H "Content-Type: application/json")
        if [ -n "$data" ]; then
            args+=(-d "$data")
        fi
    elif [ "$method" = "PUT" ]; then
        args+=(-X PUT -H "Content-Type: application/json")
        if [ -n "$data" ]; then
            args+=(-d "$data")
        fi
    elif [ "$method" = "DELETE" ]; then
        args+=(-X DELETE)
    fi

    curl "${args[@]}" "${BASE_URL}${path}"
}

# Curl an API endpoint, pass if HTTP status matches expected
curl_api_test() {
    local name="$1"
    local method="$2"
    local path="$3"
    local expected="${4:-200}"
    local data="${5:-}"

    local status
    status=$(_curl_api_status "$method" "$path" "$data")

    if [ "$status" = "$expected" ]; then
        pass_test
    else
        fail_test "$name" "Expected HTTP $expected, got $status"
    fi
}

# Curl a page, pass if HTTP 200 and body contains expected substring
curl_page_test() {
    local name="$1"
    local path="$2"
    local expected_content="$3"

    local body
    local status
    body=$(curl -s -H "Authorization: Bearer $API_KEY" -w "\n%{http_code}" "${BASE_URL}${path}")
    status=$(tail -n1 <<< "$body")
    body=$(sed '$d' <<< "$body")

    if [ "$status" != "200" ]; then
        fail_test "$name" "Expected HTTP 200, got $status"
        return
    fi

    if grep -qi "$expected_content" <<< "$body"; then
        pass_test
    else
        fail_test "$name" "Response missing expected content: $expected_content"
    fi
}

# Curl an API endpoint, pass if HTTP status is any 4xx (400-499)
curl_api_test_blocked() {
    local name="$1"
    local method="$2"
    local path="$3"
    local data="${4:-}"

    local status
    status=$(_curl_api_status "$method" "$path" "$data")

    if [[ "$status" =~ ^4[0-9]{2}$ ]]; then
        pass_test
    else
        fail_test "$name" "Expected HTTP 4xx, got $status"
    fi
}

# Curl an API endpoint with auth, pass if HTTP 200 and body contains expected substring
curl_api_body_test() {
    local name="$1"
    local path="$2"
    local expected_content="$3"

    local body
    local status
    body=$(curl -s -H "Authorization: Bearer $API_KEY" -H "X-Requested-With: XMLHttpRequest" \
        -w "\n%{http_code}" "${BASE_URL}${path}")
    status=$(tail -n1 <<< "$body")
    body=$(sed '$d' <<< "$body")

    if [ "$status" != "200" ]; then
        fail_test "$name" "Expected HTTP 200, got $status"
        return
    fi

    if grep -qi "$expected_content" <<< "$body"; then
        pass_test
    else
        fail_test "$name" "Response missing expected content: $expected_content"
    fi
}

# ---------------------------------------------------------------------------
# Prerequisite check
# ---------------------------------------------------------------------------

echo ""
echo "=== Dango Smoke Test ==="
echo "Server: $BASE_URL"
echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Check server is reachable and healthy
HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "${BASE_URL}/api/health" 2>/dev/null || echo "000")
if [ "$HEALTH_STATUS" != "200" ]; then
    echo "ERROR: Dango server not reachable at $BASE_URL (HTTP $HEALTH_STATUS)"
    echo "Make sure 'dango start' is running in your test project."
    exit 1
fi

# ---------------------------------------------------------------------------
# Category 1: Install & Import
# ---------------------------------------------------------------------------

category_start 5

run_cmd_test "import dango" python3 -c "import dango; print(dango.__version__)"
run_cmd_test "dango --help" dango --help
run_cmd_test "dango config --help" dango config --help
run_cmd_test "dango auth --help" dango auth --help
run_cmd_test "import key deps" python3 -c "import dlt; import duckdb"

category_end "1" "Install & Import"

# ---------------------------------------------------------------------------
# Category 2: CLI Operations
# ---------------------------------------------------------------------------

category_start 5

run_cmd_test "config validate" dango config validate
run_cmd_test "auth status" dango auth status
run_cmd_test "auth list-users" dango auth list-users
run_cmd_test "db status" dango db status

# change-role may not exist yet (added in R7-E) — skip gracefully
if dango auth change-role --help > /dev/null 2>&1; then
    pass_test
else
    skip_test "auth change-role --help" "not yet implemented (R7-E)"
fi

category_end "2" "CLI Operations"

# ---------------------------------------------------------------------------
# Category 3: API Endpoints
# ---------------------------------------------------------------------------

category_start 8

curl_api_test "GET /api/status" GET "/api/status"
curl_api_test "GET /api/sources" GET "/api/sources"
curl_api_test "GET /api/config" GET "/api/config"
curl_api_test "GET /api/health/platform" GET "/api/health/platform"
curl_api_test "GET /api/dbt/models" GET "/api/dbt/models"
curl_api_test "GET /api/governance/schema-drift" GET "/api/governance/schema-drift"
curl_api_test "GET /api/governance/pii" GET "/api/governance/pii"
curl_api_test "GET /api/logs?limit=5" GET "/api/logs?limit=5"

category_end "3" "API Endpoints"

# ---------------------------------------------------------------------------
# Category 4: Page Loads
# ---------------------------------------------------------------------------

category_start 11

curl_page_test "/ (Overview)" "/" "Overview"
curl_page_test "/sources" "/sources" "Sources"
curl_page_test "/models" "/models" "Models"
curl_page_test "/schedules" "/schedules" "Schedules"
curl_page_test "/catalog" "/catalog" "Catalog"
# /monitoring page route removed in M2 (merged into catalog); API still at /api/monitoring
curl_page_test "/notebooks" "/notebooks" "Notebooks"
curl_page_test "/health" "/health" "Health"
curl_page_test "/logs" "/logs" "Logs"
curl_page_test "/settings/account" "/settings/account" "Account"
curl_page_test "/settings/users" "/settings/users" "User"
curl_page_test "/settings/secrets" "/settings/secrets" "Secrets"

category_end "4" "Page Loads"

# ---------------------------------------------------------------------------
# Category 5: JS Null Guards
# ---------------------------------------------------------------------------

category_start 1

if python3 "${REPO_ROOT}/scripts/check_js_null_guards.py" > /dev/null 2>&1; then
    pass_test
else
    fail_test "check_js_null_guards.py" "Unguarded DOM lookups found (run script for details)"
fi

category_end "5" "JS Null Guards"

# ---------------------------------------------------------------------------
# Category 6: Nav Structure
# ---------------------------------------------------------------------------

category_start 1

nav_html=$(curl -s -H "Authorization: Bearer $API_KEY" "${BASE_URL}/")
nav_ok=true

# Check for 8 pipeline nav items (monitoring removed in R12-M2)
for item in "Overview" "Sources" "Models" "Schedules" "Catalog" "Query" "Dashboards" "Notebooks"; do
    if ! grep -q "$item" <<< "$nav_html"; then
        fail_test "Nav structure" "Missing nav item: $item"
        nav_ok=false
        break
    fi
done

# Check "More" dropdown is gone (target state after R7-C)
# Match the specific dropdown comment/button, not incidental "More" text
if $nav_ok && grep -q "More dropdown" <<< "$nav_html"; then
    fail_test "Nav structure" "Found 'More' dropdown — should be removed after R7-C"
    nav_ok=false
fi

if $nav_ok; then
    pass_test
fi

category_end "6" "Nav Structure"

# ---------------------------------------------------------------------------
# Category 7: R8 Regression Checks
# ---------------------------------------------------------------------------

category_start 8

# --- Static/import checks (no server needed) ---

# BUG-099: Metabase healthcheck start_period
if grep -q "start_period: 300s" "$REPO_ROOT/dango/templates/docker-compose.yml.j2"; then
    pass_test
else
    fail_test "Metabase start_period 300s (BUG-099)" "docker-compose template missing start_period: 300s"
fi

# BUG-083: Session idle timeout default
if python3 -c "from dango.auth.sessions import DEFAULT_IDLE_TIMEOUT_MINUTES; assert DEFAULT_IDLE_TIMEOUT_MINUTES == 10080" 2>/dev/null; then
    pass_test
else
    fail_test "Idle timeout 10080m (BUG-083)" "DEFAULT_IDLE_TIMEOUT_MINUTES != 10080"
fi

# BUG-102: ensure_dbt_schemas creates DB with all 4 schemas
if python3 -c "
import tempfile, os
from pathlib import Path
from dango.utils.database import ensure_dbt_schemas
import duckdb
d = tempfile.mkdtemp()
p = Path(d) / 'test.duckdb'
ensure_dbt_schemas(p)
assert p.exists(), 'DB not created'
conn = duckdb.connect(str(p), read_only=True)
schemas = [r[0] for r in conn.execute(\"SELECT schema_name FROM information_schema.schemata\").fetchall()]
conn.close()
os.unlink(p)
os.rmdir(d)
for s in ('raw', 'staging', 'intermediate', 'marts'):
    assert s in schemas, f'Missing schema: {s}'
" 2>/dev/null; then
    pass_test
else
    fail_test "ensure_dbt_schemas (BUG-102)" "Failed to create DB with all 4 schemas"
fi

# BUG-027b: spaCy must not call cli.download (causes sys.exit)
# Pass if no non-comment line calls spacy.cli.download
if grep -v "^[[:space:]]*#" "$REPO_ROOT/dango/governance/pii_detector.py" | grep -q "spacy\.cli\.download("; then
    fail_test "No spacy.cli.download (BUG-027b)" "Found spacy.cli.download() call in code"
else
    pass_test
fi

# --- Server-dependent checks ---

# BUG-066: Cache bust uses content hash (8-char hex), not timestamps
page_html=$(curl -s -H "Authorization: Bearer $API_KEY" "${BASE_URL}/")
if grep -qE '\?v=[0-9a-f]{8}' <<< "$page_html"; then
    # Also verify no timestamp-style params (10+ digits)
    if grep -qE '\?v=[0-9]{10,}' <<< "$page_html"; then
        fail_test "Cache bust hash (BUG-066)" "Found timestamp-style ?v= param (should be 8-char hex)"
    else
        pass_test
    fi
else
    fail_test "Cache bust hash (BUG-066)" "No ?v=<8-char-hex> found in page HTML"
fi

# BUG-054: Login redirect — skip with API key auth
# The /login redirect checks dango_session cookie, not Bearer tokens.
# API key auth won't trigger the redirect. This is correct browser-only behavior.
skip_test "Login redirect (BUG-054)" "requires cookie auth, not testable with API key"

# BUG-065: Mobile nav includes Activity Logs and Health links
# Reuse page_html from BUG-066 check (same page)
nav_065_ok=true
if ! grep -q "Activity Logs" <<< "$page_html"; then
    fail_test "Mobile nav (BUG-065)" "Missing 'Activity Logs' link"
    nav_065_ok=false
fi
if $nav_065_ok && ! grep -q 'href="/health"' <<< "$page_html"; then
    fail_test "Mobile nav (BUG-065)" "Missing href=\"/health\" link"
    nav_065_ok=false
fi
if $nav_065_ok; then
    pass_test
fi

# BUG-082: /api/sources includes supports_date_range capability
# Note: requires at least one configured source in the test project
sources_body=$(curl -s -H "Authorization: Bearer $API_KEY" -H "X-Requested-With: XMLHttpRequest" "${BASE_URL}/api/sources")
if [ "$sources_body" = "[]" ]; then
    skip_test "supports_date_range (BUG-082)" "no sources configured"
elif grep -q "supports_date_range" <<< "$sources_body"; then
    pass_test
else
    fail_test "supports_date_range (BUG-082)" "Field not found in /api/sources response"
fi

category_end "7" "R8 Regression Checks"

# ---------------------------------------------------------------------------
# Category 8: R9 Feature Checks
# ---------------------------------------------------------------------------

category_start 12

# Catalog endpoints
curl_api_test "GET /api/catalog/models" GET "/api/catalog/models"
curl_api_test "GET /api/catalog/summary" GET "/api/catalog/summary"
curl_api_test "GET /api/catalog/search?q=test" GET "/api/catalog/search?q=test"
curl_api_test "GET /api/catalog/lineage" GET "/api/catalog/lineage"

# Governance
curl_api_test "GET /api/governance/pii/overrides" GET "/api/governance/pii/overrides"
curl_api_test "GET /api/governance/attention" GET "/api/governance/attention"

# Notebooks — create then delete
curl_api_test "GET /api/notebooks" GET "/api/notebooks"
# Pre-cleanup: remove leftover from previous interrupted run
curl -s -o /dev/null -H "Authorization: Bearer $API_KEY" \
    -H "X-Requested-With: XMLHttpRequest" \
    -X DELETE "${BASE_URL}/api/notebooks/smoke_test_nb" 2>/dev/null
NB_CREATE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $API_KEY" \
    -H "X-Requested-With: XMLHttpRequest" \
    -X POST -H "Content-Type: application/json" \
    -d '{"name":"smoke_test_nb"}' \
    "${BASE_URL}/api/notebooks")
if [ "$NB_CREATE_STATUS" = "201" ]; then
    pass_test
    # Only delete if create succeeded
    curl_api_test "DELETE /api/notebooks/smoke_test_nb" DELETE "/api/notebooks/smoke_test_nb"
else
    fail_test "POST /api/notebooks (create)" "Expected HTTP 201, got $NB_CREATE_STATUS"
    skip_test "DELETE /api/notebooks/smoke_test_nb" "create failed"
fi

# Monitoring API (page route removed in M2, merged into catalog)
curl_api_test "GET /api/monitoring" GET "/api/monitoring"
curl_api_test "GET /api/monitoring/history" GET "/api/monitoring/history?metric=row_count&days=7"
curl_api_test "POST /api/monitoring/run" POST "/api/monitoring/run"

category_end "8" "R9 Feature Checks"

# ---------------------------------------------------------------------------
# Category 9: R10 Feature Checks
# ---------------------------------------------------------------------------

category_start 13

# --- Config mutation blocked (R10-C / BUG-175) ---
curl_api_test_blocked "POST /api/schedules blocked" POST "/api/schedules"
curl_api_test_blocked "PUT /api/schedules/test blocked" PUT "/api/schedules/test"
curl_api_test_blocked "DELETE /api/schedules/test blocked" DELETE "/api/schedules/test"
curl_api_test_blocked "POST /api/notifications/webhooks blocked" POST "/api/notifications/webhooks"
curl_api_test_blocked "DELETE /api/notifications/webhooks/test blocked" DELETE "/api/notifications/webhooks/test"

# --- PII override writes blocked (R10-H / BUG-161) ---
curl_api_test_blocked "PUT /api/governance/pii/overrides blocked" PUT "/api/governance/pii/overrides"
curl_api_test_blocked "DELETE /api/governance/pii/overrides blocked" DELETE "/api/governance/pii/overrides"

# --- Monitoring dbt_tests field (R10-G2) ---
curl_api_body_test "dbt_tests in /api/monitoring" "/api/monitoring" "dbt_tests"

# --- CLI commands (R10-K, R10-M) ---
run_cmd_test "dango dev --help" dango dev --help
run_cmd_test "dango snapshot --help" dango snapshot --help
run_cmd_test "dango snapshot list --help" dango snapshot list --help

# --- Health DuckDB capacity (R10-L) ---
curl_api_body_test "duckdb_size_bytes in /api/health/platform" "/api/health/platform" "duckdb_size_bytes"

# --- /query link (R10-J2 / BUG-173) ---
# Query nav link points directly to /metabase/question#hash (no redirect)
# The /query endpoint still exists as a JS redirect fallback (returns 200 HTML)
curl_api_test "/query → 200" GET "/query" "200"

category_end "9" "R10 Feature Checks"

# ---------------------------------------------------------------------------
# Category 10: R12 Code Checks (grep-based, no server needed)
# ---------------------------------------------------------------------------

category_start 28

# R12-A/BUG-229: State backup before pipeline.drop()
# Presence check: both backup mechanism and pipeline.drop() exist in dlt_runner.
# Ordering (backup < drop) verified by unit tests, not grep.
if grep -q "_backup_dlt_state\|backup.*state" "$REPO_ROOT/dango/ingestion/dlt_runner.py" 2>/dev/null && \
   grep -q "pipeline\.drop" "$REPO_ROOT/dango/ingestion/dlt_runner.py" 2>/dev/null; then
    pass_test
else
    fail_test "State backup before pipeline.drop (BUG-229)"
fi

# R12-A/BUG-246: Metabase :ro mount or snapshot in docker-compose
if grep -q ':ro' "$REPO_ROOT/dango/templates/docker-compose.yml.j2" 2>/dev/null; then
    pass_test
else
    fail_test "Metabase :ro volume mount (BUG-246)"
fi

# R12-A/BUG-239: No silent except-pass in deploy files
SILENT_EXCEPT=$(grep -rn 'except.*Exception' "$REPO_ROOT/dango/platform/cloud/deploy"*.py 2>/dev/null | grep -v '^\s*#' | grep 'pass$' | wc -l | tr -d ' ')
if [ "$SILENT_EXCEPT" = "0" ]; then
    pass_test
else
    fail_test "No silent except-pass in deploy (BUG-239)" "Found $SILENT_EXCEPT occurrences"
fi

# R12-B/BUG-191: Docker first-run timeout >= 600
if grep -qE '(timeout|TIMEOUT).*600|600.*timeout' "$REPO_ROOT/dango/platform/docker.py" 2>/dev/null || \
   grep -qE '(timeout|TIMEOUT).*600|600.*timeout' "$REPO_ROOT/dango/cli/commands/platform.py" 2>/dev/null; then
    pass_test
else
    fail_test "Docker first-run timeout >= 600 (BUG-191)"
fi

# R12-B/BUG-209: dbt build not dbt run
if grep -qE '"dbt".*"build"|dbt.*build' "$REPO_ROOT/dango/cli/commands/transform.py" 2>/dev/null; then
    pass_test
else
    fail_test "dbt build not dbt run (BUG-209)"
fi

# R12-B/BUG-205: Snapshot data/ path references warehouse inside data dir
if grep -qE 'data.*warehouse|data_dir.*warehouse|warehouse.*data' "$REPO_ROOT/dango/cli/commands/snapshot.py" 2>/dev/null; then
    pass_test
else
    fail_test "Snapshot data/ path (BUG-205)"
fi

# R12-B/BUG-233: Rich escape_markup usage
if grep -qE 'escape_markup|from rich\.markup import escape' "$REPO_ROOT/dango/cli/source_wizard.py" 2>/dev/null; then
    pass_test
else
    fail_test "Rich escape_markup (BUG-233)"
fi

# R12-B/BUG-192: Browser auto-open in notebook
if grep -q 'webbrowser\.open' "$REPO_ROOT/dango/cli/commands/notebook.py" 2>/dev/null; then
    pass_test
else
    fail_test "Browser auto-open notebook (BUG-192)"
fi

# R12-B/BUG-197: Catalog badge alignment (flex-shrink-0)
if grep -q 'flex-shrink-0' "$REPO_ROOT/dango/web/templates/catalog.html" 2>/dev/null; then
    pass_test
else
    fail_test "Catalog badge alignment (BUG-197)"
fi

# R12-B/BUG-221: File watcher cleanup on stop
if grep -q 'stop_file_watcher' "$REPO_ROOT/dango/cli/commands/platform.py" 2>/dev/null; then
    pass_test
else
    fail_test "File watcher cleanup (BUG-221)"
fi

# R12-C/BUG-214: Sync status watcher in lifespan
if grep -q 'sync_status_watcher' "$REPO_ROOT/dango/web/app.py" 2>/dev/null; then
    pass_test
else
    fail_test "Sync status watcher in lifespan (BUG-214)"
fi

# R12-C/BUG-230: dbt_run_all_failed event exists
if grep -rq 'dbt_run_all_failed' "$REPO_ROOT/dango/" 2>/dev/null; then
    pass_test
else
    fail_test "dbt_run_all_failed event (BUG-230)"
fi

# R12-F/BUG-195: Lock error message in dlt_runner
if grep -q 'is locked' "$REPO_ROOT/dango/ingestion/dlt_runner.py" 2>/dev/null; then
    pass_test
else
    fail_test "Lock error message (BUG-195)"
fi

# R12-F/BUG-222: Invalid SQL message in query route
if grep -q 'Invalid SQL syntax' "$REPO_ROOT/dango/web/routes/query.py" 2>/dev/null; then
    pass_test
else
    fail_test "Invalid SQL message (BUG-222)"
fi

# R12-F/BUG-228: DbtLock default timeout > 0
if python3 -c "
from dango.utils.dbt_lock import DbtLock
import inspect
sig = inspect.signature(DbtLock.acquire)
default = sig.parameters['timeout'].default
assert default > 0, f'DbtLock timeout default is {default}, expected > 0'
" 2>/dev/null; then
    pass_test
else
    fail_test "DbtLock timeout > 0 (BUG-228)"
fi

# R12-H/BUG-194: Notebook snapshot path env variable
if grep -rq 'DANGO_NOTEBOOK_DB_PATH' "$REPO_ROOT/dango/notebooks/templates/" 2>/dev/null; then
    pass_test
else
    fail_test "Notebook snapshot path env (BUG-194)"
fi

# R12-H/BUG-196: Notebook idle timeout > 900
if python3 -c "
from dango.notebooks.manager import _IDLE_TIMEOUT
assert _IDLE_TIMEOUT > 900, f'_IDLE_TIMEOUT is {_IDLE_TIMEOUT}, expected > 900'
" 2>/dev/null; then
    pass_test
else
    fail_test "Notebook idle timeout > 900 (BUG-196)"
fi

# R12-H/BUG-193: CLI notebook acquires lock before writing
if grep -qE 'acquire_lock|DbtLock|notebook_lock' "$REPO_ROOT/dango/cli/commands/notebook.py" 2>/dev/null; then
    pass_test
else
    fail_test "CLI notebook acquires lock (BUG-193)"
fi

# R12-J/BUG-235: IP-based lockout (record_failed_login tracks client IP)
if grep -qE 'client_ip|ip_address|remote_addr' "$REPO_ROOT/dango/auth/lockout.py" 2>/dev/null; then
    pass_test
else
    fail_test "IP-based lockout (BUG-235)"
fi

# R12-I/BUG-237: Deploy email confirm
if grep -rqiE 'confirm.*email|email.*confirm' "$REPO_ROOT/dango/platform/cloud/deploy"*.py 2>/dev/null || \
   grep -rqiE 'confirm.*email|email.*confirm' "$REPO_ROOT/dango/cli/commands/deploy"*.py 2>/dev/null; then
    pass_test
else
    fail_test "Deploy email confirm (BUG-237)"
fi

# R12-I/BUG-245: Metabase timeout >= 300 in cloud templates
if grep -qE 'timeout.*300|300s|300.*timeout' "$REPO_ROOT/dango/templates/docker-compose.yml.j2" 2>/dev/null || \
   grep -qE 'timeout.*300|300s|300.*timeout' "$REPO_ROOT/dango/templates/nginx.conf.j2" 2>/dev/null; then
    pass_test
else
    fail_test "Metabase timeout >= 300 (BUG-245)"
fi

# R12-I/BUG-243: Cloud status systemd check
if grep -qE 'systemctl|systemd' "$REPO_ROOT/dango/cli/commands/platform.py" 2>/dev/null || \
   grep -rqE 'systemctl|systemd' "$REPO_ROOT/dango/platform/cloud/server_status.py" 2>/dev/null; then
    pass_test
else
    fail_test "Cloud status systemd (BUG-243)"
fi

# R12-B/BUG-200: Catalog back button fallback (goToList called from restoreFromUrl)
# Check that restoreFromUrl function body contains goToList as fallback
# Uses a simpler check: restoreFromUrl and goToList both exist in the file,
# and goToList appears after restoreFromUrl (within the function).
if python3 -c "
with open('$REPO_ROOT/dango/web/templates/catalog.html') as f:
    text = f.read()
idx_restore = text.index('restoreFromUrl')
# goToList must appear after restoreFromUrl definition
idx_go = text.index('goToList()', idx_restore)
assert idx_go > idx_restore, 'goToList not found after restoreFromUrl'
" 2>/dev/null; then
    pass_test
else
    fail_test "Catalog back button fallback (BUG-200)"
fi

# R12-D/BUG-217: Source cards collapse (showAllSources toggle)
if grep -qE 'showAllSources|show_all_sources|toggleSources' "$REPO_ROOT/dango/web/templates/catalog.html" 2>/dev/null; then
    pass_test
else
    fail_test "Source cards collapse (BUG-217)"
fi

# R12-D/BUG-220: Source name onclick opens detail
if grep -q 'openSourceDetail' "$REPO_ROOT/dango/web/templates/sources.html" 2>/dev/null || \
   grep -q 'openSourceDetail' "$REPO_ROOT/dango/web/static/js/app.js" 2>/dev/null; then
    pass_test
else
    fail_test "Source name onclick (BUG-220)"
fi

# R12-E/BUG-203: Freshness removed from monitoring (shown on /sources instead)
# Verify no freshness metrics are generated in templates.py
if ! grep -q 'value_expression.*_dlt_load_id' "$REPO_ROOT/dango/analysis/templates.py" 2>/dev/null; then
    pass_test
else
    fail_test "Freshness removed from monitoring (BUG-203)"
fi

# R12-G/BUG-227: Trigger Now simplified (no Advanced options in trigger dialog)
# The fix removes "Advanced" toggle from trigger/sync dialog.
# Check that no trigger-adjacent code references "Advanced" or "advanced options".
if grep -qiE 'triggerAdvanced|trigger.*advanced|advanced.*trigger|advancedOptions.*trigger' \
   "$REPO_ROOT/dango/web/static/js/app.js" 2>/dev/null; then
    fail_test "Trigger Now simplified (BUG-227)" "Found Advanced references in trigger dialog"
else
    pass_test
fi

# R12-G/BUG-226: Schedule SPACE toggle hint
if grep -qiE 'SPACE to toggle|space.*toggle' "$REPO_ROOT/dango/cli/commands/schedule.py" 2>/dev/null; then
    pass_test
else
    fail_test "Schedule SPACE hint (BUG-226)"
fi

category_end "10" "R12 Code Checks"

# ---------------------------------------------------------------------------
# Category 11: R12 CLI Checks (command existence)
# ---------------------------------------------------------------------------

category_start 8

run_cmd_test "dango remote auth --help" dango remote auth --help
run_cmd_test "dango model remove --help" dango model remove --help
run_cmd_test "dango notebook open --help" dango notebook open --help
run_cmd_test "dango snapshot add --help" dango snapshot add --help
run_cmd_test "dango source edit --help" dango source edit --help
run_cmd_test "dango schedule add --help" dango schedule add --help
run_cmd_test "dango notebook --help" dango notebook --help
run_cmd_test "dango sync --help" dango sync --help

category_end "11" "R12 CLI Checks"

# ---------------------------------------------------------------------------
# Category 12: R12 Server Checks (API/page content)
# ---------------------------------------------------------------------------

category_start 3

# R12-D/BUG-199: Test name tooltips on catalog page
curl_page_test "Catalog test tooltips (BUG-199)" "/catalog" "title="

# R12-D/BUG-218: PII badge tooltip (title attribute on PII span)
curl_page_test "PII badge tooltip (BUG-218)" "/catalog" "Auto-detected:"

# BUG-202 (Run Analysis spinner) and BUG-216 (Tests grouped) were on /monitoring page,
# which was removed in M2 (merged into catalog). Monitoring API still tested in category 8.

# R12-G/BUG-211: Sync history duration in API response
curl_api_body_test "Sync history duration (BUG-211)" "/api/sources" "last_sync_duration_seconds"

category_end "12" "R12 Server Checks"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

TOTAL=$((TOTAL_PASS + TOTAL_FAIL + TOTAL_SKIP))

echo ""
printf "TOTAL: %d/%d PASS (%d FAIL, %d SKIP)\n" "$TOTAL_PASS" "$TOTAL" "$TOTAL_FAIL" "$TOTAL_SKIP"
printf "Duration: %dm %ds\n" "$MINUTES" "$SECONDS"
echo ""

if [ "$TOTAL_FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
