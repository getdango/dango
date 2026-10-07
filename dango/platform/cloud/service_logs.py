"""dango/platform/cloud/service_logs.py

Resolve and read remote service logs (CLI and MCP share this).

Compose v2 names containers ``<project>-<service>-<n>``, so the Metabase container is
found by its Compose labels instead of assuming a fixed name.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

LOG_COMMANDS: dict[str, str] = {
    "dango": "journalctl -u dango-web --no-pager",
    "caddy": "journalctl -u caddy --no-pager",
}
# Services that run as Compose containers rather than systemd units.
CONTAINER_SERVICES: frozenset[str] = frozenset({"metabase"})
LOG_SERVICES: tuple[str, ...] = ("dango", "caddy", "metabase")

NO_CONTAINER_MSG = "No {service} container found on the server."


def find_service_container(ssh: Any, service: str, project_dir: str) -> str | None:
    """Return the Compose container name for ``service`` in this project, or None.

    Only ``<project>-<service>[-<n>]`` matches, so another project's container or one such
    as ``metabase-proxy`` is never returned. A running container wins over a stopped one.
    """
    from dango.platform.cloud.backup import get_remote_compose_project_name

    project = get_remote_compose_project_name(ssh, project_dir)
    wanted = re.compile(rf"{re.escape(project)}-{re.escape(service)}(-\d+)?")
    filters = (
        f"--filter {shlex.quote('label=com.docker.compose.project=' + project)} "
        f"--filter {shlex.quote('label=com.docker.compose.service=' + service)}",
        f"--filter {shlex.quote(f'name=^{project}-{service}')}",
    )
    for all_states in ("", "-a "):  # running first, then stopped
        for flt in filters:
            res = ssh.exec_command(
                f"docker ps {all_states}{flt} --format '{{{{.Names}}}}'", check=False
            )
            if not res.success:
                continue
            for line in (res.stdout or "").splitlines():
                if wanted.fullmatch(line.strip()):
                    return line.strip()
    return None


def build_log_command(
    ssh: Any, service: str, tail_n: int, project_dir: str, *, follow: bool = False
) -> str | None:
    """Return the shell command that reads ``service`` logs, or None if its container is absent."""
    if service in CONTAINER_SERVICES:
        name = find_service_container(ssh, service, project_dir)
        if name is None:
            return None
        cmd = f"docker logs --tail {tail_n} {shlex.quote(name)}"
        return cmd + (" -f" if follow else "")
    cmd = f"{LOG_COMMANDS[service]} -n {tail_n}"
    return cmd + (" -f" if follow else "")
