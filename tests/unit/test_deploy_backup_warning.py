"""tests/unit/test_deploy_backup_warning.py

Tests for the backup-status warning printed by `_print_deploy_success()`
(dango/cli/commands/deploy.py). BYOS deploys always pass backup_enabled=False,
which previously printed nothing about backups at all. This covers the new
warning shown for that case, and confirms the existing "enabled" message is
unaffected.
"""

import pytest


@pytest.mark.unit
class TestBackupWarning:
    def test_warning_shown_when_backup_disabled(self, capsys):
        from dango.cli.commands.deploy import _print_deploy_success

        _print_deploy_success(
            url="https://example.com",
            ip="1.2.3.4",
            admin_email="admin@example.com",
            warnings=[],
            backup_enabled=False,
        )
        captured = capsys.readouterr()
        assert "Backups" in captured.out
        assert "Not configured" in captured.out or "server only" in captured.out

    def test_no_warning_when_backup_enabled(self, capsys):
        from dango.cli.commands.deploy import _print_deploy_success

        _print_deploy_success(
            url="https://example.com",
            ip="1.2.3.4",
            admin_email="admin@example.com",
            warnings=[],
            backup_enabled=True,
        )
        captured = capsys.readouterr()
        assert "Enabled" in captured.out
        assert "Not configured" not in captured.out
