"""tests/unit/test_post_sync.py

Tests for the post-sync dispatcher (dango/utils/post_sync.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.utils.post_sync import dispatch_post_sync_hooks


@pytest.mark.unit
class TestDispatchPostSyncHooks:
    """Unit tests for dispatch_post_sync_hooks."""

    def test_calls_all_hooks(self, tmp_path: Path):
        """All four hooks are called with the correct sources."""
        sources = ["shopify", "stripe"]
        with (
            patch("dango.utils.post_sync._run_profiling") as mock_prof,
            patch("dango.utils.post_sync._run_drift_detection") as mock_drift,
            patch("dango.utils.post_sync._run_pii_scan") as mock_pii,
            patch("dango.utils.post_sync._run_analysis") as mock_analysis,
        ):
            dispatch_post_sync_hooks(project_root=tmp_path, sources=sources)

        mock_prof.assert_called_once_with(tmp_path, sources)
        mock_drift.assert_called_once_with(tmp_path, sources)
        mock_pii.assert_called_once_with(tmp_path, sources)
        mock_analysis.assert_called_once_with(tmp_path, sources)

    def test_empty_sources_skips_hooks(self, tmp_path: Path):
        """No hooks are called when sources list is empty."""
        with patch("dango.utils.post_sync._run_profiling") as mock_prof:
            dispatch_post_sync_hooks(project_root=tmp_path, sources=[])

        mock_prof.assert_not_called()

    def test_stubs_are_noop(self, tmp_path: Path):
        """Calling with real stubs does not raise."""
        dispatch_post_sync_hooks(project_root=tmp_path, sources=["test_source"])
