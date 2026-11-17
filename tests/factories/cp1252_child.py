"""tests/factories/cp1252_child.py

Run a snippet in a cp1252-like (non-UTF-8) child interpreter that records every text-mode open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT = "café — ✓ über"

# Records (mode, encoding, newline) of every text-mode open, so Windows-only behaviour
# (cp1252 default encoding, CRLF translation) stays observable on POSIX.
_PRELUDE = """
import builtins, io, json, sys
from pathlib import Path

from dango.utils.stdio import ensure_safe_stdio

ensure_safe_stdio()
TEXT = __TEXT__
root = Path(sys.argv[1])
_opens = []
_real_open = io.open

def _spy(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, *a, **kw):
    if "b" not in mode:
        _opens.append({"mode": mode, "encoding": encoding, "newline": newline})
    return _real_open(file, mode, buffering, encoding, errors, newline, *a, **kw)

io.open = _spy
builtins.open = _spy
""".replace("__TEXT__", ascii(TEXT))


def run_child(body: str, tmp_path: Path, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run ``body`` and return its ``result`` dict (must be JSON, ASCII-safe)."""
    script = _PRELUDE + textwrap.dedent(body) + "\nprint(json.dumps(result))\n"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "cp1252",
        "LC_ALL": "C",
        "DANGO_TELEMETRY": "0",
        "DANGO_PROJECT_ROOT": str(project),
        **(extra_env or {}),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script, str(project)],
        env=env,
        capture_output=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[-1500:]
    result: dict[str, Any] = json.loads(proc.stdout.decode("ascii").strip().splitlines()[-1])
    return result
