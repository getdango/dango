#!/usr/bin/env bash
# scripts/run_pip_audit.sh
#
# Run pip-audit with the dated ignore list (.github/pip-audit-ignores.txt).
# Fails if any ignore has expired (scripts/check_audit_ignores.py) or if pip-audit
# reports an advisory that is not on the list. Used by CI and the weekly audit.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
list="$root/.github/pip-audit-ignores.txt"

python "$root/scripts/check_audit_ignores.py" "$list"

args=()
while read -r id _; do
  case "$id" in
    '' | '#'*) continue ;;
  esac
  args+=(--ignore-vuln "$id")
done <"$list"

pip-audit ${args[@]+"${args[@]}"}
