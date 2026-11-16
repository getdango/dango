"""tests/integration/t44a_qualification/sitecustomize.py

Qualification-only Python audit observer, enabled only for the notebook child tree.
"""

import json
import os
import sys
from pathlib import Path


def observe(event, args):
    """Retain executable identity without argv/env and refuse excluded helper programs."""
    if event not in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        return
    executable = os.fsdecode(args[0])
    refused = "/_lsp/" in executable or Path(executable).name in {"uv", "uvx", "node", "copilot"}
    record(event, executable, refused)
    assert not refused, "Excluded notebook helper executable"


def record(event, executable, refused=False):
    """Keep all audit allocations inside the declared task report bound."""
    path = Path("/work/notebook-processes.jsonl")
    assert not path.exists() or path.stat().st_size < 1024**2
    row = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "event": event,
        "executable": executable,
        "refused": refused,
    }
    with path.open("a") as output:
        output.write(json.dumps(row) + "\n")


if os.environ.get("T44A_PROCESS_AUDIT") == "1":
    record("python_start", sys.executable)
    sys.addaudithook(observe)
