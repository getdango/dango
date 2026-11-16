"""dango/platform/local/routing.py

Read-only access to the shared-nginx routing registry (``~/.dango/routing.json``).
"""

from __future__ import annotations

import json
from pathlib import Path


def routing_file_path() -> Path:
    """Return the shared-nginx routing registry path (``~/.dango/routing.json``).

    Same file as ``NetworkConfig.routing_file`` in ``network.py``.
    """
    return Path.home() / ".dango" / "routing.json"


def get_registered_domains(project_path: Path) -> frozenset[str]:
    """Return the local nginx domains registered for *project_path* (lower-cased).

    Read-only: creates nothing. Missing or unreadable registry -> empty set.
    """
    try:
        with open(routing_file_path()) as f:
            routing = json.load(f)
        target = project_path.resolve()
        return frozenset(
            str(info["domain"]).lower()
            for info in routing.get("projects", {}).values()
            if info.get("domain")
            and info.get("project_path")
            and Path(info["project_path"]).resolve() == target
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return frozenset()
