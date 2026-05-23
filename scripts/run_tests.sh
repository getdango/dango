#!/bin/bash
# Guard script: prevent running pytest when another instance is already active.
# This avoids zombie processes, DuckDB lock conflicts, and flaky test results.

# Use [p]ytest regex trick to avoid pgrep matching itself
if pgrep -f "[p]ytest" > /dev/null 2>&1; then
    PIDS=$(pgrep -f "[p]ytest" | head -1)
    echo "ERROR: pytest is already running (PID: $PIDS)"
    echo "Wait for it to finish or kill it: kill $PIDS"
    exit 1
fi
exec pytest "$@"
