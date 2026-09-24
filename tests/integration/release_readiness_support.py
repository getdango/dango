"""tests/integration/release_readiness_support.py

Bounded Metabase restart readiness checks for release-readiness scenarios.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


def start_services_and_wait_for_metabase(
    manager: Any,
    project_root: Path,
    metabase_url: str,
    base_url: str,
    session: Any,
) -> None:
    """Start the scoped stack, then restore its authenticated Metabase proxy."""
    from dango.visualization.metabase import (
        _wait_for_metabase_log_ready,
        wait_for_metabase_ready,
    )

    compose_project_name = manager.compose_project_name
    container_name = f"{compose_project_name}-metabase-1"
    since = datetime.now(timezone.utc).isoformat()
    assert manager.start_services(), "The real restart failed"

    if not _wait_for_metabase_log_ready(container_name, since, max_wait_seconds=90) and not (
        wait_for_metabase_ready(metabase_url, timeout=30)
    ):
        _fail_metabase_restart_readiness(project_root, compose_project_name, container_name, since)

    try:
        proxy_response = session.get(f"{base_url}/metabase/", timeout=15)
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary for this integration test
        pytest.fail(f"Authenticated Metabase proxy did not recover after restart: {exc}")
    assert proxy_response.status_code == 200, (
        "Authenticated Metabase proxy did not recover after restart: "
        f"{proxy_response.status_code} {proxy_response.text[:1000]}"
    )


def _fail_metabase_restart_readiness(
    project_root: Path,
    compose_project_name: str,
    container_name: str,
    since: str,
) -> None:
    """Fail a post-restart readiness wait while retaining Docker diagnostics."""
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project_name
    try:
        logs = subprocess.run(
            ["docker", "logs", "--since", since, container_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        log_output = f"{logs.stdout}{logs.stderr}"
    except (OSError, subprocess.SubprocessError) as exc:
        log_output = f"<could not collect Metabase logs: {exc}>"
    try:
        status = subprocess.run(
            ["docker", "compose", "-f", str(project_root / "docker-compose.yml"), "ps"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        status_output = f"{status.stdout}{status.stderr}"
    except (OSError, subprocess.SubprocessError) as exc:
        status_output = f"<could not collect Compose status: {exc}>"
    pytest.fail(
        "Metabase did not become ready after the release-readiness restart.\n"
        f"Compose status:\n{status_output}\n"
        f"Metabase logs since restart:\n{log_output}"
    )
