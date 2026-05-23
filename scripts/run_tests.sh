#!/bin/bash
# Guard script: prevent running pytest when another instance is already active.
# This avoids zombie processes, DuckDB lock conflicts, and flaky test results.

if pgrep -f "pytest" > /dev/null 2>&1; then
    echo "ERROR: pytest is already running (PID: $(pgrep -f pytest))"
    echo "Wait for it to finish or kill it: kill $(pgrep -f pytest)"
    exit 1
fi
exec pytest "$@"
