"""tests/unit/test_platform_imports.py

Verifies backward-compatible shim imports work correctly after platform/ restructure.
"""

import pytest


@pytest.mark.unit
class TestBackwardsCompatibleImports:
    def test_old_network_import(self):
        """NetworkConfig is importable from the legacy dango.platform.network path."""
        from dango.platform.network import NetworkConfig  # noqa: F401

    def test_old_watcher_import(self):
        """MultiTargetWatcher is importable from the legacy dango.platform.watcher path."""
        from dango.platform.watcher import MultiTargetWatcher  # noqa: F401

    def test_old_watcher_lifecycle_import(self):
        """get_watcher_status is importable from the legacy dango.platform.watcher_lifecycle path."""
        from dango.platform.watcher_lifecycle import get_watcher_status  # noqa: F401

    def test_old_watcher_runner_import(self):
        """main is importable from the legacy dango.platform.watcher_runner path."""
        from dango.platform.watcher_runner import main  # noqa: F401

    def test_platform_init_import(self):
        """DockerManager is importable from dango.platform (unchanged top-level export)."""
        from dango.platform import DockerManager  # noqa: F401


@pytest.mark.unit
class TestNewCanonicalImports:
    def test_local_network_import(self):
        """NetworkConfig is importable from the new canonical dango.platform.local.network path."""
        from dango.platform.local.network import NetworkConfig  # noqa: F401

    def test_local_watcher_import(self):
        """MultiTargetWatcher is importable from the new canonical dango.platform.local.watcher path."""
        from dango.platform.local.watcher import MultiTargetWatcher  # noqa: F401

    def test_local_watcher_lifecycle_import(self):
        """get_watcher_status is importable from the new canonical dango.platform.local.watcher_lifecycle path."""
        from dango.platform.local.watcher_lifecycle import get_watcher_status  # noqa: F401

    def test_cloud_package_import(self):
        """dango.platform.cloud is importable as an empty package."""
        import dango.platform.cloud  # noqa: F401

    def test_common_startup_import(self):
        """run_pending_migrations is importable from dango.platform.common.startup."""
        from dango.platform.common.startup import run_pending_migrations  # noqa: F401

    def test_cloud_config_import(self):
        """CloudConfig is importable from dango.config.models."""
        from dango.config.models import CloudConfig  # noqa: F401
