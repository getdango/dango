"""dango/utils/git_info.py

Git repository info and deployment guardrails.

Pure utility — subprocess calls to git, no dango dependencies.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GitInfo:
    """Snapshot of the local git repository state."""

    commit_sha: str | None = None
    branch: str | None = None
    is_clean: bool | None = None
    remote_url: str | None = None
    is_git_repo: bool = False


@dataclass(frozen=True)
class GitGuardrailResult:
    """Result of pre-deployment git checks."""

    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stdout or None on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def collect_git_info(project_root: Path) -> GitInfo:
    """Collect current git repository state.

    Returns a GitInfo with is_git_repo=False if not in a git repo
    or git is not installed.
    """
    # Check if we're in a git repo
    check = _run_git(["rev-parse", "--is-inside-work-tree"], project_root)
    if check != "true":
        return GitInfo()

    commit_sha = _run_git(["rev-parse", "HEAD"], project_root)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    remote_url = _run_git(["config", "--get", "remote.origin.url"], project_root)

    # Check for uncommitted changes (staged + unstaged + untracked)
    status = _run_git(["status", "--porcelain"], project_root)
    is_clean = status == "" if status is not None else None

    return GitInfo(
        commit_sha=commit_sha,
        branch=branch,
        is_clean=is_clean,
        remote_url=remote_url,
        is_git_repo=True,
    )


def check_git_guardrails(
    git_info: GitInfo,
    *,
    expected_branch: str = "main",
    allow_dirty: bool = False,
    allow_branch: bool = False,
) -> GitGuardrailResult:
    """Check git state against deployment requirements.

    Returns a result with passed=True if all checks pass (or are overridden).
    Warnings are non-blocking; errors are blocking (unless overridden).
    """
    warnings: list[str] = []
    errors: list[str] = []

    if not git_info.is_git_repo:
        warnings.append("Not a git repository — skipping git checks.")
        return GitGuardrailResult(passed=True, warnings=warnings)

    # Branch check
    if git_info.branch and git_info.branch != expected_branch:
        if allow_branch:
            warnings.append(f"Deploying from '{git_info.branch}' (expected '{expected_branch}').")
        else:
            errors.append(f"On branch '{git_info.branch}', expected '{expected_branch}'.")

    # Dirty working tree check
    if git_info.is_clean is False:
        if allow_dirty:
            warnings.append("Working tree has uncommitted changes.")
        else:
            errors.append("Working tree has uncommitted changes.")

    # No upstream warning (non-blocking)
    if git_info.commit_sha and git_info.branch:
        # A detached HEAD or missing remote isn't an error
        if git_info.branch == "HEAD":
            warnings.append("Detached HEAD — no branch tracking.")

    passed = len(errors) == 0
    return GitGuardrailResult(passed=passed, warnings=warnings, errors=errors)
