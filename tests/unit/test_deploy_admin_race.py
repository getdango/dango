"""tests/unit/test_deploy_admin_race.py

1.0.13-T17: the deploy's remote admin script must wait for the server to
create/migrate ``auth.db`` (``systemctl start`` returns immediately), then fall
back to ``apply_all_pending``.  Each test EXECUTES the generated script in a
subprocess against real sqlite in ``tmp_path`` (no network, no SSH).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision
from dango.cli.commands.deploy_provision import (
    AdminCreationError,
    _build_admin_script,
    _create_admin_and_enable_auth,
)
from dango.exceptions import CloudProvisioningError

EMAIL = "admin@example.com"
HASH = "$2b$12$placeholderplaceholderplaceholderplaceholderplaceholde"
REPO_ROOT = str(Path(deploy_provision.__file__).resolve().parents[3])


def _run(project: Path, wait: int, email: str = EMAIL, pw_hash: str = HASH):
    script = _build_admin_script(email, pw_hash, str(project), wait)
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": REPO_ROOT, "PATH": "/usr/bin:/bin"},
        check=False,
    )
    return proc, time.monotonic() - start


def _users(project: Path) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(project / ".dango" / "auth.db")
    try:
        return conn.execute(
            "SELECT email, password_hash, must_change_password FROM users"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / ".dango").mkdir()
    return tmp_path


@pytest.mark.unit
class TestAdminScriptMatrix:
    def test_row1_waits_for_server_to_create_auth_db(self, project: Path) -> None:
        from dango.migrations import apply_all_pending

        threading.Timer(3.0, apply_all_pending, args=(project,)).start()
        proc, elapsed = _run(project, wait=30)
        assert proc.returncode == 0, proc.stderr
        assert elapsed >= 3.0
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_row2_existing_users_table_does_not_wait(self, project: Path) -> None:
        from dango.migrations import apply_all_pending

        apply_all_pending(project)
        proc, elapsed = _run(project, wait=60)
        assert proc.returncode == 0, proc.stderr
        assert elapsed < 30
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_row3_falls_back_to_migrations_when_table_never_appears(self, project: Path) -> None:
        proc, elapsed = _run(project, wait=3)
        assert proc.returncode == 0, proc.stderr
        assert elapsed >= 3.0
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_row4_existing_user_gets_password_updated(self, project: Path) -> None:
        from dango.migrations import apply_all_pending

        apply_all_pending(project)
        assert _run(project, wait=5)[0].returncode == 0
        new_hash = HASH.replace("placeholder", "differentxxxx", 1)
        conn = sqlite3.connect(project / ".dango" / "auth.db")
        conn.execute("UPDATE users SET must_change_password = 0")
        conn.commit()
        conn.close()
        proc, _ = _run(project, wait=5, pw_hash=new_hash)
        assert proc.returncode == 0, proc.stderr
        rows = _users(project)
        assert len(rows) == 1
        assert rows[0][1] == new_hash
        assert rows[0][2] == 1

    def test_row5_zero_byte_auth_db_is_not_a_users_table(self, project: Path) -> None:
        (project / ".dango" / "auth.db").write_bytes(b"")
        proc, elapsed = _run(project, wait=3)
        assert proc.returncode == 0, proc.stderr
        assert elapsed >= 3.0
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_waits_for_last_migration_not_just_users_table(self, project: Path) -> None:
        from dango.migrations import get_migrations_base_dir
        from dango.migrations.runner import MigrationRunner

        runner = MigrationRunner(
            project / ".dango" / "auth.db", "auth", get_migrations_base_dir() / "auth"
        )
        pending = runner.get_pending()
        assert len(pending) >= 6

        def apply_stepwise() -> None:
            time.sleep(0.5)
            for migration in pending:
                runner.apply_one(migration)
                time.sleep(1.5)

        t = threading.Thread(target=apply_stepwise)
        t.start()
        proc, elapsed = _run(project, wait=60)
        t.join()
        assert proc.returncode == 0, proc.stderr
        assert elapsed >= 0.5 + 1.5 * (len(pending) - 1)
        assert runner.get_pending() == []
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_timeout_with_partial_migrations_applies_the_rest(self, project: Path) -> None:
        from dango.migrations import get_migrations_base_dir
        from dango.migrations.runner import MigrationRunner

        runner = MigrationRunner(
            project / ".dango" / "auth.db", "auth", get_migrations_base_dir() / "auth"
        )
        runner.apply_one(runner.get_pending()[0])
        proc, elapsed = _run(project, wait=3)
        assert proc.returncode == 0, proc.stderr
        assert elapsed >= 3.0
        assert runner.get_pending() == []
        assert [u[0] for u in _users(project)] == [EMAIL]

    def test_existing_rows_are_unchanged(self, project: Path) -> None:
        from dango.auth.database import create_user, get_user_by_email
        from dango.auth.models import Role, User
        from dango.migrations import apply_all_pending

        apply_all_pending(project)
        db = project / ".dango" / "auth.db"
        other = create_user(
            db, User(email="other@example.com", password_hash="h0", role=Role.ADMIN)
        )
        proc, _ = _run(project, wait=5)
        assert proc.returncode == 0, proc.stderr
        kept = get_user_by_email(db, "other@example.com")
        assert kept is not None
        assert (kept.id, kept.password_hash) == (other.id, "h0")
        assert sorted(u[0] for u in _users(project)) == [EMAIL, "other@example.com"]

    def test_row6_unrelated_failure_surfaces_real_error(self, tmp_path: Path) -> None:
        # ".dango" is a regular file, so auth.db cannot be created/opened.
        (tmp_path / ".dango").write_text("not a directory")
        proc, _ = _run(tmp_path, wait=1)
        assert proc.returncode != 0
        assert "Traceback" in proc.stderr
        assert "transient" not in proc.stderr.lower()

    def test_no_plaintext_password_and_default_path(self) -> None:
        script = _build_admin_script(EMAIL, HASH)
        assert "'/srv/dango/project'" in script
        assert "wait_seconds = 120" in script


@pytest.mark.unit
class TestAdminStepErrorHandling:
    def _ssh(self, exit_code: int, stderr: str = "") -> MagicMock:
        ssh = MagicMock()
        ssh.exec_command.return_value = MagicMock(exit_code=exit_code, stderr=stderr, stdout="")
        return ssh

    def test_ssh_timeout_covers_the_wait_and_failure_is_admin_error(self) -> None:
        ssh = self._ssh(1, "sqlite3.OperationalError: no such table: users")
        with pytest.raises(AdminCreationError, match="no such table: users"):
            _create_admin_and_enable_auth(ssh, EMAIL, "password123")
        assert ssh.exec_command.call_args_list[0].kwargs["timeout"] >= 180
        assert issubclass(AdminCreationError, CloudProvisioningError)

    @pytest.mark.parametrize(
        ("exc", "expect_transient"),
        [
            (AdminCreationError("Admin account creation failed:\nboom"), False),
            (RuntimeError("API"), True),
        ],
    )
    def test_handler_wording(self, tmp_path: Path, monkeypatch, exc, expect_transient) -> None:
        from dango.cli.commands.deploy_provision import run_provisioning
        from dango.cli.commands.deploy_wizard import WizardConfig

        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
        (tmp_path / ".dango").mkdir()
        config = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email=EMAIL,
            admin_password="strongpassword123",
            skip_oauth=True,
            enable_backups=False,
            monthly_cost=24,
        )
        client = MagicMock()
        client.upload_ssh_key.return_value = {"id": 1}
        droplet = {"id": 2, "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]}}
        fake_console = MagicMock()
        ssh = MagicMock()
        ssh.exec_command.return_value = MagicMock(exit_code=0, stdout="4", stderr="")
        with (
            patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
            patch("dango.platform.cloud.provisioning.provision_droplet", return_value=droplet),
            patch(
                "dango.platform.cloud.firewall.create_default_firewall", return_value={"id": "fw"}
            ),
            patch("dango.platform.cloud.server_setup.setup_server"),
            patch("dango.platform.cloud.file_sync.sync_project_files"),
            patch("dango.platform.cloud.provisioning.save_provisioning_metadata"),
            patch("dango.cli.commands.deploy_wizard._safe_confirm", return_value=True),
            patch.object(deploy_provision, "_start_services"),
            patch.object(deploy_provision, "_create_admin_and_enable_auth", side_effect=exc),
            patch.object(deploy_provision, "time"),
            patch.object(deploy_provision, "console", fake_console),
            pytest.raises(CloudProvisioningError),
        ):
            run_provisioning(tmp_path, config)
        printed = " ".join(str(c) for c in fake_console.print.call_args_list)
        assert ("transient infrastructure" in printed) is expect_transient, printed
