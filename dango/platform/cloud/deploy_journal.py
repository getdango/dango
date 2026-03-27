"""dango/platform/cloud/deploy_journal.py

Append-only JSONL deployment journal for tracking deployment history.

Records git commit, branch, deployer identity, duration, and file changes
for every deployment.  All write operations follow the never-fail pattern:
errors are logged but never raised.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager


_REMOTE_JOURNAL = "/srv/dango/project/.dango/state/deployments.jsonl"


@dataclass(frozen=True)
class DeploymentRecord:
    """Single deployment event."""

    timestamp: str
    deployer: str
    success: bool
    git_commit: str | None = None
    git_branch: str | None = None
    git_clean: bool | None = None
    git_remote_url: str | None = None
    dango_version: str | None = None
    files_synced: list[str] = field(default_factory=list)
    models_changed: list[str] = field(default_factory=list)
    models_added: list[str] = field(default_factory=list)
    models_removed: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = False
    error: str | None = None


def write_local_journal(project_root: Path, record: DeploymentRecord) -> None:
    """Append a deployment record to the local journal.

    Path: ``<project_root>/.dango/state/deployments.jsonl``

    Never raises — errors are printed as warnings.
    """
    journal_path = project_root / ".dango" / "state" / "deployments.jsonl"
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(journal_path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Failed to write local deployment journal: {exc}")


def write_remote_journal(ssh: SSHManager, record: DeploymentRecord) -> None:
    """Append a deployment record to the remote journal via SSH.

    Path: ``/srv/dango/project/.dango/state/deployments.jsonl``

    Never raises — errors are silently ignored.
    """
    try:
        json_str = json.dumps(asdict(record))
        parent = str(Path(_REMOTE_JOURNAL).parent)
        cmd = f"mkdir -p {parent} && echo {shlex.quote(json_str)} >> {_REMOTE_JOURNAL}"
        ssh.exec_command(cmd)
    except Exception:  # noqa: BLE001
        pass


def read_local_journal(project_root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Read the last *limit* entries from the local deployment journal.

    Returns entries newest-first.  Empty list on missing file or errors.
    """
    journal_path = project_root / ".dango" / "state" / "deployments.jsonl"
    if not journal_path.exists():
        return []
    try:
        lines = journal_path.read_text().strip().splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries
    except Exception:  # noqa: BLE001
        return []


def read_remote_journal(ssh: SSHManager, limit: int = 20) -> list[dict[str, Any]]:
    """Read the last *limit* entries from the remote deployment journal.

    Returns entries newest-first.  Empty list on missing file or errors.
    """
    try:
        result = ssh.exec_command(f"tail -{limit} {_REMOTE_JOURNAL} 2>/dev/null")
        if not result.success or not result.stdout.strip():
            return []
        entries: list[dict[str, Any]] = []
        for line in result.stdout.strip().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries
    except Exception:  # noqa: BLE001
        return []


def get_latest_deployment(ssh: SSHManager) -> dict[str, Any] | None:
    """Read the most recent deployment record from the remote journal.

    Returns ``None`` if the journal is empty or unreadable.
    """
    try:
        result = ssh.exec_command(f"tail -1 {_REMOTE_JOURNAL} 2>/dev/null")
        if not result.success or not result.stdout.strip():
            return None
        entry: dict[str, Any] = json.loads(result.stdout.strip())
        return entry
    except (json.JSONDecodeError, Exception):  # noqa: BLE001
        return None
