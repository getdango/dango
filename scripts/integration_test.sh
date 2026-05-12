#!/usr/bin/env bash
# scripts/integration_test.sh — v1.0 (2026-05-12)
#
# CI-like gate for R12 PRs. Runs linting, type-checking, unit tests,
# and static validation scripts. No running server needed.
#
# Usage:
#   ./scripts/integration_test.sh
#   ./scripts/integration_test.sh --help
#
# Prerequisites:
#   - Activated virtualenv (source venv/bin/activate)
#   - Run from repository root (where pyproject.toml lives)

set -euo pipefail

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $0"
    echo ""
    echo "Run CI-like integration checks (no server needed)."
    echo ""
    echo "Stages:"
    echo "  [1/5] ruff check"
    echo "  [2/5] ruff format --check"
    echo "  [3/5] mypy"
    echo "  [4/5] pytest unit tests"
    echo "  [5/5] Static validation scripts"
    echo ""
    echo "Requires: activated virtualenv, run from repo root."
    exit 0
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    echo "ERROR: No virtualenv active. Run: source venv/bin/activate"
    exit 1
fi

if [[ ! -f "pyproject.toml" ]]; then
    echo "ERROR: Must run from repository root (where pyproject.toml is)"
    exit 1
fi

START_TIME=$(date +%s)
STAGES_PASSED=0
TOTAL_STAGES=5

echo ""
echo "=== Dango Integration Test ==="
echo "Date: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Python: $(python3 --version 2>&1)"
echo "Venv: $VIRTUAL_ENV"
echo ""

# ---------------------------------------------------------------------------
# [1/5] Ruff lint
# ---------------------------------------------------------------------------

echo "[1/$TOTAL_STAGES] ruff check ..."
ruff check dango/ tests/ scripts/
STAGES_PASSED=$((STAGES_PASSED + 1))
echo "  PASS"
echo ""

# ---------------------------------------------------------------------------
# [2/5] Ruff format
# ---------------------------------------------------------------------------

echo "[2/$TOTAL_STAGES] ruff format --check ..."
ruff format --check dango/ tests/ scripts/
STAGES_PASSED=$((STAGES_PASSED + 1))
echo "  PASS"
echo ""

# ---------------------------------------------------------------------------
# [3/5] Mypy
# ---------------------------------------------------------------------------

echo "[3/$TOTAL_STAGES] mypy dango/ ..."
mypy dango/
STAGES_PASSED=$((STAGES_PASSED + 1))
echo "  PASS"
echo ""

# ---------------------------------------------------------------------------
# [4/5] Unit tests
# ---------------------------------------------------------------------------

echo "[4/$TOTAL_STAGES] pytest tests/unit/ ..."
pytest tests/unit/ -x --tb=short -q
STAGES_PASSED=$((STAGES_PASSED + 1))
echo "  PASS"
echo ""

# ---------------------------------------------------------------------------
# [5/5] Static checks
# ---------------------------------------------------------------------------

echo "[5/$TOTAL_STAGES] Static validation scripts ..."
python3 scripts/check_version_alignment.py
echo "  check_version_alignment.py — OK"
python3 scripts/check_file_sizes.py --all
echo "  check_file_sizes.py — OK"
python3 scripts/check_js_null_guards.py
echo "  check_js_null_guards.py — OK"
STAGES_PASSED=$((STAGES_PASSED + 1))
echo "  PASS"
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo "=== ALL $STAGES_PASSED/$TOTAL_STAGES STAGES PASSED ==="
printf "Duration: %dm %ds\n" "$MINUTES" "$SECONDS"
echo ""
