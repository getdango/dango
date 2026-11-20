"""tests/factories/journey_installer_fakes.py

A stand-in for install.sh used by the first-user journey `--installer` tests: it records the
variables and directory it was started with, then builds venv/bin/{dango,python} from the fakes
that run_journey puts in FAKE_BIN. POSIX-only.
"""

from __future__ import annotations

import stat
from pathlib import Path

FAKE_INSTALLER = r"""#!/bin/bash
set -e
env | grep '^DANGO_INSTALL_' | sort > install-env.txt
pwd > install-cwd.txt
# Any spelling of the variable counts: Windows reports environment names in upper case.
{ env | grep -i '^PSExecutionPolicyPreference=' || echo "<unset>"; } > install-ps-policy.txt
if [ "${FAKE_INSTALLER_FAIL:-}" = 1 ]; then
    echo "installer: boom"
    exit 1
fi
if [ "${FAKE_INSTALLER_NO_DANGO:-}" = 1 ]; then
    exit 0
fi
mkdir -p venv/bin
cp "$FAKE_BIN/dango" venv/bin/dango
cp "$FAKE_BIN/python" venv/bin/python
# What `importlib.metadata` would report for the installed getdango (read by the fake venv python).
dist="$FAKE_STATE_DIR/installed-dist.json"
spec="${DANGO_INSTALL_SPEC:-}"
if [ "${FAKE_INSTALLER_IGNORE_SPEC:-}" = 1 ]; then
    printf '{"version": "1.0.14", "direct_url": null}\n' > "$dist"
else
    case "$spec" in
        *==*) printf '{"version": "%s", "direct_url": null}\n' "${spec##*==}" > "$dist" ;;
        *://*) printf '{"version": "1.0.15", "direct_url": {"url": "%s"}}\n' "$spec" > "$dist" ;;
        *) printf '{"version": "1.0.15", "direct_url": {"url": "file://%s"}}\n' "$spec" > "$dist" ;;
    esac
fi
"""


def make_fake_installer(directory: Path) -> Path:
    """Write the fake installer script into `directory` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake-install.sh"
    script.write_text(FAKE_INSTALLER, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script
