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
"""


def make_fake_installer(directory: Path) -> Path:
    """Write the fake installer script into `directory` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake-install.sh"
    script.write_text(FAKE_INSTALLER, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script
