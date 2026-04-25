#!/usr/bin/env bash
# scripts/smoke_test.sh — v1.1 (2026-04-25)
#
# Automated smoke test for a running Dango instance.
# Requires: dango start running in a test project, venv activated.
#
# Usage:
#   ./scripts/smoke_test.sh [BASE_URL]
#   ./scripts/smoke_test.sh --help
#
# Environment variables:
#   DANGO_BASE_URL       — Server URL (default: http://localhost:8800)
#   DANGO_ADMIN_EMAIL    — Admin email (default: admin@localhost)
#   DANGO_ADMIN_PASSWORD — Admin password (required, no default)

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
    echo "Environment variables:"
    echo "  DANGO_BASE_URL       Server URL"
    echo "  DANGO_ADMIN_EMAIL    Admin email (default: admin@localhost)"
    echo "  DANGO_ADMIN_PASSWORD Admin password (required)"
    exit 0
fi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL="${1:-${DANGO_BASE_URL:-http://localhost:8800}}"
ADMIN_EMAIL="${DANGO_ADMIN_EMAIL:-admin@localhost}"
ADMIN_PASSWORD="${DANGO_ADMIN_PASSWORD:-}"

if [ -z "$ADMIN_PASSWORD" ]; then
    echo "ERROR: DANGO_ADMIN_PASSWORD environment variable is required."
    echo "Usage: DANGO_ADMIN_PASSWORD=<password> $0 [BASE_URL]"
    exit 1
fi

COOKIE_JAR=$(mktemp)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

trap 'rm -f "$COOKIE_JAR"' EXIT

# Validate ADMIN_EMAIL format
if ! echo "$ADMIN_EMAIL" | grep -qE '^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'; then
    echo "ERROR: DANGO_ADMIN_EMAIL ('$ADMIN_EMAIL') is not a valid email address."
    exit 1
fi

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
    printf "[%s/6] %-28s %d/%d %s%s\n" "$num" "$name" "$CAT_PASS" "$CAT_TOTAL" "$status" "$extra"
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

# Curl an API endpoint, pass if HTTP status matches expected
curl_api_test() {
    local name="$1"
    local method="$2"
    local path="$3"
    local expected="${4:-200}"
    local data="${5:-}"

    local args=(-s -o /dev/null -w "%{http_code}" -b "$COOKIE_JAR")
    args+=(-H "X-Requested-With: XMLHttpRequest")

    if [ "$method" = "POST" ]; then
        args+=(-X POST -H "Content-Type: application/json")
        if [ -n "$data" ]; then
            args+=(-d "$data")
        fi
    fi

    local status
    status=$(curl "${args[@]}" "${BASE_URL}${path}")

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
    body=$(curl -s -b "$COOKIE_JAR" -w "\n%{http_code}" "${BASE_URL}${path}")
    status=$(echo "$body" | tail -n1)
    body=$(echo "$body" | sed '$d')

    if [ "$status" != "200" ]; then
        fail_test "$name" "Expected HTTP 200, got $status"
        return
    fi

    if echo "$body" | grep -qi "$expected_content"; then
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

category_start 9

# Login to get session cookie
LOGIN_OK=false
login_status=$(curl -s -c "$COOKIE_JAR" -o /dev/null -w "%{http_code}" \
    -X POST "${BASE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -H "X-Requested-With: XMLHttpRequest" \
    -d "{\"email\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}")

if [ "$login_status" = "200" ]; then
    pass_test
    LOGIN_OK=true
else
    fail_test "POST /api/auth/login" "Expected HTTP 200, got $login_status"
fi

if $LOGIN_OK; then
    curl_api_test "GET /api/status" GET "/api/status"
    curl_api_test "GET /api/sources" GET "/api/sources"
    curl_api_test "GET /api/config" GET "/api/config"
    curl_api_test "GET /api/health/platform" GET "/api/health/platform"
    curl_api_test "GET /api/dbt/models" GET "/api/dbt/models"
    curl_api_test "GET /api/governance/schema-drift" GET "/api/governance/schema-drift"
    curl_api_test "GET /api/governance/pii" GET "/api/governance/pii"
    curl_api_test "GET /api/logs?limit=5" GET "/api/logs?limit=5"
else
    echo "    BLOCKED: Skipping 8 API tests — login failed"
    for _ in $(seq 8); do skip_test "API endpoint" "login failed"; done
fi

category_end "3" "API Endpoints"

# ---------------------------------------------------------------------------
# Category 4: Page Loads
# ---------------------------------------------------------------------------

category_start 12

if $LOGIN_OK; then
    curl_page_test "/ (Overview)" "/" "Overview"
    curl_page_test "/sources" "/sources" "Sources"
    curl_page_test "/models" "/models" "Models"
    curl_page_test "/schedules" "/schedules" "Schedules"
    curl_page_test "/catalog" "/catalog" "Catalog"
    curl_page_test "/insights" "/insights" "Insights"
    curl_page_test "/notebooks" "/notebooks" "Notebooks"
    curl_page_test "/health" "/health" "Health"
    curl_page_test "/logs" "/logs" "Logs"
    curl_page_test "/settings/account" "/settings/account" "Account"
    curl_page_test "/settings/users" "/settings/users" "User"
    curl_page_test "/settings/secrets" "/settings/secrets" "Secrets"
else
    echo "    BLOCKED: Skipping 12 page tests — login failed"
    for _ in $(seq 12); do skip_test "Page load" "login failed"; done
fi

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

if $LOGIN_OK; then
    nav_html=$(curl -s -b "$COOKIE_JAR" "${BASE_URL}/")
    nav_ok=true

    # Check for 9 pipeline nav items (target state after R7-C)
    for item in "Overview" "Sources" "Models" "Schedules" "Catalog" "Query" "Dashboards" "Notebooks" "Insights"; do
        if ! echo "$nav_html" | grep -q "$item"; then
            fail_test "Nav structure" "Missing nav item: $item"
            nav_ok=false
            break
        fi
    done

    # Check "More" dropdown is gone (target state after R7-C)
    # Match the specific dropdown comment/button, not incidental "More" text
    if $nav_ok && echo "$nav_html" | grep -q "More dropdown"; then
        fail_test "Nav structure" "Found 'More' dropdown — should be removed after R7-C"
        nav_ok=false
    fi

    if $nav_ok; then
        pass_test
    fi
else
    skip_test "Nav structure" "login failed"
fi

category_end "6" "Nav Structure"

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
