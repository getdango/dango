"""dango/utils/post_sync.py

Post-sync dispatcher for data governance hooks.

Called after a successful ``dango sync`` to run profiling, drift detection,
PII scanning, and automated analysis on freshly-loaded data.  Each hook is
a stub that will be populated by subsequent Phase 7 tasks.
"""

from __future__ import annotations

from pathlib import Path

from dango.logging import get_logger

logger = get_logger(__name__)


def dispatch_post_sync_hooks(
    project_root: Path,
    sources: list[str],
) -> None:
    """Run post-sync hooks for successfully synced sources.

    Invokes each hook in order: profiling, drift detection, PII scanning,
    analysis.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.
    """
    if not sources:
        return

    logger.info("post_sync_hooks_start", sources=sources)

    _run_profiling(project_root, sources)
    _run_drift_detection(project_root, sources)
    _run_pii_scan(project_root, sources)
    _run_analysis(project_root, sources)

    logger.info("post_sync_hooks_complete", sources=sources)


def _run_profiling(project_root: Path, sources: list[str]) -> None:
    """Profile columns for freshly synced sources.

    Populated by P7-001.
    """


def _run_drift_detection(project_root: Path, sources: list[str]) -> None:
    """Detect schema drift for freshly synced sources.

    Populated by P7-005.
    """


def _run_pii_scan(project_root: Path, sources: list[str]) -> None:
    """Scan for PII in freshly synced sources.

    Populated by P7-006.
    """


def _run_analysis(project_root: Path, sources: list[str]) -> None:
    """Run automated analysis on freshly synced sources.

    Populated by P7-011.
    """
