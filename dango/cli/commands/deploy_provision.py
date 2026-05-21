"""dango/cli/commands/deploy_provision.py

Provisioning orchestration for ``dango deploy`` — Step 10.

Orchestrates the full deployment pipeline: SSH key generation, droplet
provisioning, firewall, server setup, file sync, secrets push, admin
creation, optional backup setup, and service start with health check.

On failure, ``_ResourceTracker.cleanup()`` tears down any DO resources
that were created (no orphans).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dango.cli import console
from dango.cli.commands.deploy_wizard import _EMAIL_RE, BYOSConfig, WizardConfig
from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger

_logger = get_logger(__name__)


@dataclass
class _ResourceTracker:
    """Tracks provisioned DO resources for cleanup on failure."""

    client: Any = None  # DigitalOceanClient
    droplet_id: int | None = None
    firewall_id: str | None = None
    ssh_key_id: int | None = None
    spaces_bucket: str | None = None
    spaces_client: Any = None  # SpacesClient

    def cleanup(self) -> list[str]:
        """Delete all tracked resources. Returns list of error messages."""
        errors: list[str] = []

        if self.droplet_id and self.client:
            try:
                self.client.delete_droplet(self.droplet_id)
            except Exception as exc:
                errors.append(f"Droplet {self.droplet_id}: {exc}")

        if self.firewall_id and self.client:
            try:
                self.client.delete_firewall(self.firewall_id)
            except Exception as exc:
                errors.append(f"Firewall {self.firewall_id}: {exc}")

        if self.ssh_key_id and self.client:
            try:
                self.client.delete_ssh_key(self.ssh_key_id)
            except Exception as exc:
                errors.append(f"SSH key {self.ssh_key_id}: {exc}")

        if self.spaces_bucket and self.spaces_client:
            try:
                self.spaces_client.delete_bucket()
            except Exception as exc:
                errors.append(f"Spaces bucket {self.spaces_bucket}: {exc}")

        return errors


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProvisionResult:
    """Result of a successful provisioning run."""

    droplet_ip: str
    droplet_id: int
    firewall_id: str
    ssh_key_id: int
    domain: str | None = None
    url: str = ""
    admin_password: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class BYOSResult:
    """Result of a successful BYOS setup run."""

    server_ip: str
    domain: str | None = None
    url: str = ""
    admin_password: str = ""
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main provisioning function
# ---------------------------------------------------------------------------


def run_provisioning(
    project_root: Path,
    config: WizardConfig,
    *,
    non_interactive: bool = False,
) -> ProvisionResult:
    """Execute the full provisioning pipeline (sub-steps 1-10).

    Args:
        project_root: Path to the local Dango project root.
        config: Wizard configuration from steps 1-9.
        non_interactive: When ``True``, auto-confirm prompts (BUG-252).

    Returns:
        ``ProvisionResult`` with deployment details.

    Raises:
        CloudProvisioningError: On unrecoverable failure (after cleanup).
    """
    from dango.platform.cloud.digitalocean import DigitalOceanClient
    from dango.platform.cloud.ssh import SSHManager

    # BUG-251: Resolve project_root so Path(".").name is never empty
    project_root = project_root.resolve()

    tracker = _ResourceTracker()

    try:
        client = DigitalOceanClient()
        tracker.client = client

        # --- Sub-step 1: Generate SSH key ---
        _status("Generating SSH key pair...")
        key_path = project_root / ".dango" / "cloud_key"
        ssh = SSHManager(key_path=key_path)
        public_key = ssh.generate_key_pair()

        # --- Sub-step 2: Upload SSH key to DO ---
        _status("Uploading SSH key to DigitalOcean...")
        key_name = f"dango-{project_root.name}-{int(time.time())}"
        key_data = client.upload_ssh_key(key_name, public_key)
        ssh_key_id: int = key_data["id"]
        tracker.ssh_key_id = ssh_key_id

        # --- Sub-step 3: Provision droplet ---
        _status("Provisioning droplet (this takes ~60s)...")
        from dango.platform.cloud.provisioning import provision_droplet

        hostname = f"dango-{project_root.name}"[:63]  # DO limit
        droplet = provision_droplet(
            client,
            name=hostname,
            region=config.region,
            size=config.size_slug,
            ssh_key_ids=[ssh_key_id],
        )
        droplet_id: int = droplet["id"]
        tracker.droplet_id = droplet_id

        # Extract IP
        droplet_ip = _extract_ip(droplet)

        # --- Sub-step 4: Create firewall ---
        _status("Creating firewall...")
        from dango.platform.cloud.firewall import create_default_firewall

        fw = create_default_firewall(client, droplet_id)
        firewall_id: str = fw["id"]
        tracker.firewall_id = firewall_id

        # --- Sub-step 5: Server setup ---
        _status("Setting up server (installs Docker, Caddy, Python, etc.)...")
        from dango.platform.cloud.server_setup import (
            resolve_install_source,
            setup_server,
        )

        install_source = resolve_install_source()
        ssh.connect(droplet_ip, username="root")
        try:
            setup_result = setup_server(
                ssh,
                on_progress=_setup_progress,
                domain=config.domain,
                install_source=install_source,
            )
            if setup_result.warnings:
                for w in setup_result.warnings:
                    console.print(f"  [yellow]Warning:[/yellow] {w}")
        finally:
            ssh.disconnect()

        # --- Sub-step 6: Sync project files ---
        _status("Syncing project files...")
        from dango.platform.cloud.file_sync import sync_project_files

        ssh.connect(droplet_ip, username="root")
        try:
            sync_project_files(
                ssh,
                project_root,
                remote_host=droplet_ip,
                on_progress=_setup_progress,
            )
        finally:
            ssh.disconnect()

        # Fix ownership — file sync uploads as root, dbt/dango-web run as dango.
        # Pre-create dbt/target so Docker's bind mount doesn't recreate it as root.
        ssh.connect(droplet_ip, username="root")
        try:
            ssh.exec_command("mkdir -p /srv/dango/project/dbt/target", timeout=10)
            ssh.exec_command("chown -R dango:dango /srv/dango/project", timeout=30)
        finally:
            ssh.disconnect()

        # --- Sub-step 6b: Generate dbt profiles.yml for cloud ---
        _status("Generating dbt profiles...")
        ssh.connect(droplet_ip, username="root")
        try:
            _generate_cloud_profiles(ssh, project_root, config.size_slug)
        finally:
            ssh.disconnect()

        # --- Sub-step 7: Push secrets ---
        warnings: list[str] = []
        # Secrets confirmation moved to wizard (push_secrets field).
        if config.push_secrets:
            _status("Pushing secrets to server...")
            ssh.connect(droplet_ip, username="root")
            try:
                _push_secrets(ssh, project_root)
            finally:
                ssh.disconnect()

        # --- Sub-step 8: (moved after service start — see sub-step 10b) ---

        # --- Sub-step 9: Setup backups (if opted in) ---
        if config.enable_backups and config.spaces_access_key and config.spaces_secret_key:
            _status("Setting up automated backups...")
            ssh.connect(droplet_ip, username="root")
            try:
                _setup_backups(
                    ssh,
                    client,
                    project_root,
                    config,
                    tracker,
                )
            finally:
                ssh.disconnect()

        # --- Save provisioning metadata ---
        _status("Saving deployment configuration...")
        from dango.platform.cloud.provisioning import save_provisioning_metadata

        save_provisioning_metadata(
            project_root,
            droplet_id=droplet_id,
            droplet_ip=droplet_ip,
            region=config.region,
            size=config.size_slug,
        )

        # Save additional metadata (firewall, ssh key, domain)
        _save_extra_metadata(
            project_root,
            firewall_id=firewall_id,
            ssh_key_id=ssh_key_id,
            domain=config.domain,
            spaces_config=_build_spaces_config(config, tracker),
        )

        # --- Sub-step 10: Pre-build Docker images ---
        # Build Metabase Docker image BEFORE starting services so dango-web
        # starts fast and the health check passes.  Without this, the Docker
        # build happens during dango-web lifespan (10+ min on first deploy),
        # causing 502s and health check failures.
        _status("Building Docker images (this may take a few minutes)...")
        ssh.connect(droplet_ip, username="root")
        try:
            import hashlib

            compose_proj = (
                f"dango-{hashlib.md5(b'/srv/dango/project', usedforsecurity=False).hexdigest()[:8]}"
            )
            ssh.exec_command(
                f"cd /srv/dango/project && COMPOSE_PROJECT_NAME={compose_proj} "
                "sudo -u dango docker compose build",
                timeout=900,
            )
        finally:
            ssh.disconnect()

        # --- Sub-step 10b: Start services ---
        _status("Starting services...")
        ssh.connect(droplet_ip, username="root")
        try:
            _start_services(ssh)
        finally:
            ssh.disconnect()

        # --- Sub-step 10b: Create admin + enable auth ---
        # Must run AFTER services start: the lifespan ensure_admin may create
        # a default admin with a random password.  This step updates it to the
        # wizard-chosen password (handles UserExistsError via update_user).
        _status("Creating admin account and enabling auth...")
        ssh.connect(droplet_ip, username="root")
        try:
            _create_admin_and_enable_auth(ssh, config.admin_email, config.admin_password)
        finally:
            ssh.disconnect()

        # --- Sub-step 10c: Trigger Metabase setup ---
        # Restart the service so lifespan setup_metabase_if_needed runs with
        # the correct admin email (DANGO_ADMIN_EMAIL) in the environment.
        _status("Configuring Metabase...")
        ssh.connect(droplet_ip, username="root")
        try:
            _trigger_metabase_setup(ssh, config.admin_email)
        finally:
            ssh.disconnect()

        url = f"https://{config.domain}" if config.domain else f"http://{droplet_ip}"
        _status("Waiting for platform to become ready...")
        health_ok = _health_check(url, timeout=180, interval=5)
        if not health_ok:
            warnings.append(f"Health check failed. Server may still be starting. Try: {url}")

        # Wait for Metabase to be ready (Java startup takes ~2 min)
        _status("Waiting for Metabase to initialize...")
        ssh.connect(droplet_ip, username="root")
        try:
            _wait_for_metabase(ssh, timeout=180)
        finally:
            ssh.disconnect()

        return ProvisionResult(
            droplet_ip=droplet_ip,
            droplet_id=droplet_id,
            firewall_id=firewall_id,
            ssh_key_id=ssh_key_id,
            domain=config.domain,
            url=url,
            admin_password=config.admin_password,
            warnings=warnings,
        )

    except Exception as exc:
        # BUG-122: Handle empty exception messages (e.g. SSH timeout)
        err_msg = str(exc) or f"{type(exc).__name__} (no detail)"
        console.print(f"\n[red]Provisioning failed:[/red] {err_msg}")
        console.print(
            "\n[dim]This is usually a transient infrastructure issue (not a Dango bug)."
            "\nRun [bold]dango deploy[/bold] to retry with a fresh server.[/dim]"
        )
        console.print("Cleaning up resources...")
        errors = tracker.cleanup()
        if errors:
            console.print("[yellow]Cleanup errors:[/yellow]")
            for err in errors:
                console.print(f"  - {err}")
        else:
            console.print("[green]All resources cleaned up.[/green]")
        raise CloudProvisioningError(err_msg) from exc


# ---------------------------------------------------------------------------
# BYOS setup function
# ---------------------------------------------------------------------------


def run_byos_setup(
    project_root: Path,
    config: BYOSConfig,
    *,
    non_interactive: bool = False,
) -> BYOSResult:
    """Execute server setup for BYOS deployments (no cloud provisioning).

    Reuses the same server setup, file sync, secrets push, and admin
    creation logic as DO provisioning — the server setup is cloud-agnostic.

    Args:
        project_root: Path to the local Dango project root.
        config: BYOS configuration from the wizard.
        non_interactive: When ``True``, auto-confirm prompts (BUG-252).

    Returns:
        ``BYOSResult`` with deployment details.

    Raises:
        CloudProvisioningError: On unrecoverable failure.
    """
    from dango.platform.cloud.file_sync import sync_project_files
    from dango.platform.cloud.server_setup import setup_server
    from dango.platform.cloud.ssh import SSHManager

    # BUG-251: Resolve project_root so Path(".").name is never empty
    project_root = project_root.resolve()

    warnings: list[str] = []
    key_path = Path(config.ssh_key_path).expanduser()
    ssh = SSHManager(key_path=key_path)

    try:
        # --- Sub-step 1: Server setup (16 steps + UFW) ---
        _status("Setting up server (installs Docker, Caddy, Python, etc.)...")
        from dango.platform.cloud.server_setup import resolve_install_source

        install_source = resolve_install_source()
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            setup_result = setup_server(
                ssh,
                on_progress=_setup_progress,
                domain=config.domain,
                setup_ufw=True,
                install_source=install_source,
            )
            if setup_result.warnings:
                for w in setup_result.warnings:
                    console.print(f"  [yellow]Warning:[/yellow] {w}")
        finally:
            ssh.disconnect()

        # --- Sub-step 2: Sync project files ---
        _status("Syncing project files...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            sync_project_files(
                ssh,
                project_root,
                remote_host=config.server_ip,
                on_progress=_setup_progress,
            )
        finally:
            ssh.disconnect()

        # Fix ownership — file sync uploads as root/ssh_user, dbt/dango-web run as dango.
        # Pre-create dbt/target so Docker's bind mount doesn't recreate it as root.
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            ssh.exec_command("mkdir -p /srv/dango/project/dbt/target", timeout=10)
            ssh.exec_command("chown -R dango:dango /srv/dango/project", timeout=30)
        finally:
            ssh.disconnect()

        # --- Sub-step 2b: Generate dbt profiles.yml for cloud ---
        _status("Generating dbt profiles...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _generate_cloud_profiles(ssh, project_root)
        finally:
            ssh.disconnect()

        # --- Sub-step 3: Push secrets ---
        # Secrets confirmation moved to wizard (push_secrets field).
        if config.push_secrets:
            _status("Pushing secrets to server...")
            ssh.connect(config.server_ip, username=config.ssh_user)
            try:
                _push_secrets(ssh, project_root)
            finally:
                ssh.disconnect()

        # --- Sub-step 4: (moved after service start — see sub-step 6b) ---

        # --- Sub-step 5: Save cloud.yml ---
        _status("Saving deployment configuration...")
        from dango.config.loader import ConfigLoader
        from dango.config.models import CloudConfig

        loader = ConfigLoader(project_root)
        cloud_cfg = CloudConfig(
            provider="byos",
            droplet_ip=config.server_ip,
            ssh_key_path=str(key_path),
            domain=config.domain,
        )
        loader.save_cloud_config(cloud_cfg)

        # --- Sub-step 6: Pre-build Docker images ---
        _status("Building Docker images (this may take a few minutes)...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            import hashlib

            compose_proj = (
                f"dango-{hashlib.md5(b'/srv/dango/project', usedforsecurity=False).hexdigest()[:8]}"
            )
            ssh.exec_command(
                f"cd /srv/dango/project && COMPOSE_PROJECT_NAME={compose_proj} "
                "sudo -u dango docker compose build",
                timeout=900,
            )
        finally:
            ssh.disconnect()

        # --- Sub-step 6a: Start services + health check ---
        _status("Starting services...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _start_services(ssh)
        finally:
            ssh.disconnect()

        # --- Sub-step 6b: Create admin + enable auth ---
        # Must run AFTER services start (see DO path comment for rationale).
        _status("Creating admin account and enabling auth...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _create_admin_and_enable_auth(ssh, config.admin_email, config.admin_password)
        finally:
            ssh.disconnect()

        # --- Sub-step 6c: Trigger Metabase setup ---
        _status("Configuring Metabase...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _trigger_metabase_setup(ssh, config.admin_email)
        finally:
            ssh.disconnect()

        url = f"https://{config.domain}" if config.domain else f"http://{config.server_ip}"
        _status("Waiting for platform to become ready...")
        health_ok = _health_check(url, timeout=180, interval=5)
        if not health_ok:
            warnings.append(f"Health check failed. Server may still be starting. Try: {url}")

        _status("Waiting for Metabase to initialize...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _wait_for_metabase(ssh, timeout=180)
        finally:
            ssh.disconnect()

        return BYOSResult(
            server_ip=config.server_ip,
            domain=config.domain,
            url=url,
            admin_password=config.admin_password,
            warnings=warnings,
        )

    except CloudProvisioningError:
        raise
    except Exception as exc:
        # BUG-122: Handle empty exception messages (e.g. SSH timeout)
        err_msg = str(exc) or f"{type(exc).__name__} (no detail)"
        console.print(f"\n[red]BYOS setup failed:[/red] {err_msg}")
        console.print(
            "[dim]Server setup steps are idempotent — "
            "run 'dango deploy destroy' then 'dango deploy' to retry.[/dim]"
        )
        raise CloudProvisioningError(err_msg) from exc


# ---------------------------------------------------------------------------
# Sub-step helpers
# ---------------------------------------------------------------------------


def _status(msg: str) -> None:
    """Print a provisioning status message with timestamp."""
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    console.print(f"\n[dim][{ts}][/dim] [bold blue]>>>[/bold blue] {msg}")


def _setup_progress(step: str, status: str) -> None:
    """Progress callback for setup_server / sync_project_files."""
    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
    if status == "done":
        console.print(f"    [dim][{ts}][/dim] [green]Done:[/green] {step}")
    elif status == "skipped":
        console.print(f"    [dim][{ts}] Skipped:[/dim] {step}")


def _extract_ip(droplet: dict[str, Any]) -> str:
    """Extract public IPv4 from droplet dict."""
    networks = droplet.get("networks", {})
    for net in networks.get("v4", []):
        if net.get("type") == "public":
            ip = net.get("ip_address")
            if ip:
                return str(ip)
    raise RuntimeError("Droplet has no public IPv4 address")


def _push_secrets(
    ssh: Any,
    project_root: Path,
) -> None:
    """Push .dlt/secrets.toml and .env to remote server."""
    secrets_path = project_root / ".dlt" / "secrets.toml"
    env_path = project_root / ".env"

    pushed_paths: list[str] = []
    if secrets_path.exists():
        content = secrets_path.read_text()
        ssh.write_remote_file("/srv/dango/project/.dlt/secrets.toml", content, mode=0o600)
        pushed_paths.append("/srv/dango/project/.dlt/secrets.toml")

    if env_path.exists():
        content = env_path.read_text()
        ssh.write_remote_file("/srv/dango/project/.env", content, mode=0o600)
        pushed_paths.append("/srv/dango/project/.env")

    if pushed_paths:
        ssh.exec_command(f"chown dango:dango {' '.join(pushed_paths)} 2>/dev/null; true")


def _create_admin_and_enable_auth(
    ssh: Any,
    email: str,
    password: str,
) -> None:
    """Create admin user and enable auth on remote server."""
    # Validate email (shell injection prevention)
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email format: {email}")

    # Hash password locally — never send plaintext to remote server
    from dango.auth.security import hash_password

    pw_hash = hash_password(password)

    # Build Python script and base64-encode it (avoids shell injection)
    script = (
        "import sys, os\n"
        "sys.path.insert(0, '/srv/dango/project')\n"
        "os.chdir('/srv/dango/project')\n"
        "from pathlib import Path\n"
        "from dango.auth.database import create_user, get_user_by_email, update_user\n"
        "from dango.auth.models import Role, User, UserUpdate\n"
        "from dango.exceptions import UserExistsError\n"
        f"email = {email!r}\n"
        f"pw_hash = {pw_hash!r}\n"
        "db_path = Path('.dango/auth.db')\n"
        "# DB already initialized by server lifespan — just create/update user\n"
        "user = User(email=email, password_hash=pw_hash, role=Role.ADMIN, must_change_password=True)\n"
        "try:\n"
        "    create_user(db_path, user)\n"
        "except UserExistsError:\n"
        "    existing = get_user_by_email(db_path, email)\n"
        "    if existing:\n"
        "        update_user(db_path, existing.id, UserUpdate(password_hash=pw_hash, email=email, must_change_password=True))\n"
    )
    encoded = base64.b64encode(script.encode()).decode()

    result = ssh.exec_command(
        f"sudo -u dango /srv/dango/venv/bin/python3 -c "
        f"\"import base64; exec(base64.b64decode('{encoded}'))\"",
        timeout=30,
    )
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Admin account creation failed:\n{result.stderr or result.stdout}"
        )

    # Persist admin email for systemd service (BUG-100)
    ssh.exec_command(
        f"grep -q '^DANGO_ADMIN_EMAIL=' /srv/dango/project/.env 2>/dev/null "
        f"|| echo 'DANGO_ADMIN_EMAIL={email}' >> /srv/dango/project/.env"
    )
    ssh.exec_command("chown dango:dango /srv/dango/project/.env 2>/dev/null; true")

    # Enable auth
    ssh.write_remote_file("/srv/dango/project/.dango/auth.yml", "enabled: true\n", mode=0o644)
    ssh.exec_command("chown dango:dango /srv/dango/project/.dango/auth.yml")

    # Write cloud-specific auth timeouts (30-day session, 60-min idle)
    timeout_script = (
        "import sys, os\n"
        "sys.path.insert(0, '/srv/dango/project')\n"
        "os.chdir('/srv/dango/project')\n"
        "from pathlib import Path\n"
        "import yaml\n"
        "config_path = Path('.dango/project.yml')\n"
        "if config_path.exists():\n"
        "    config_data = yaml.safe_load(config_path.read_text()) or {}\n"
        "    config_data.setdefault('auth', {})\n"
        "    config_data['auth']['session_max_days'] = 30\n"
        "    config_data['auth']['idle_timeout_minutes'] = 60\n"
        "    config_path.write_text(yaml.dump(config_data, default_flow_style=False, sort_keys=False))\n"
    )
    encoded_timeout = base64.b64encode(timeout_script.encode()).decode()
    ssh.exec_command(
        f"sudo -u dango /srv/dango/venv/bin/python -c "
        f"\"import base64; exec(base64.b64decode('{encoded_timeout}'))\"",
        timeout=15,
    )

    # Ensure the entire .dango/ directory is owned by the dango user so it can
    # create files like dbt.lock in .dango/state/ at runtime.
    ssh.exec_command("chown -R dango:dango /srv/dango/project/.dango")


def _trigger_metabase_setup(ssh: Any, admin_email: str) -> None:
    """Trigger Metabase setup on the remote server.

    The deploy script sets ``DANGO_ADMIN_EMAIL`` in the server ``.env`` and
    restarts the ``dango-web`` service so that the lifespan
    ``setup_metabase_if_needed`` runs with the correct admin email already
    in the environment.
    """
    # Ensure admin email is in server .env for Metabase setup
    ssh.exec_command(
        f"grep -q '^DANGO_ADMIN_EMAIL=' /srv/dango/project/.env 2>/dev/null "
        f"|| echo 'DANGO_ADMIN_EMAIL={admin_email}' >> /srv/dango/project/.env"
    )
    ssh.exec_command("chown dango:dango /srv/dango/project/.env 2>/dev/null; true")

    # Restart dango-web so lifespan re-runs setup_metabase_if_needed
    result = ssh.exec_command("systemctl restart dango-web", timeout=120)
    if result.exit_code != 0:
        _logger.warning(
            "metabase_setup_restart_failed",
            stderr=result.stderr,
        )


def _setup_backups(
    ssh: Any,
    client: Any,
    project_root: Path,
    config: WizardConfig,
    tracker: _ResourceTracker,
) -> None:
    """Set up automated backups: create bucket, write credentials, enable timer."""
    from dango.platform.cloud._server_templates import (
        SYSTEMD_BACKUP_SERVICE,
        SYSTEMD_BACKUP_TIMER,
    )
    from dango.platform.cloud.spaces import SpacesClient

    bucket_name = f"dango-backup-{project_root.name}-{config.region}"
    spaces = SpacesClient(
        bucket=bucket_name,
        region=config.region,
        access_key=config.spaces_access_key,
        secret_key=config.spaces_secret_key,
    )
    tracker.spaces_client = spaces
    tracker.spaces_bucket = bucket_name

    # Create bucket (idempotent)
    spaces.create_bucket()

    # Write Spaces credentials to remote .env
    env_lines = (
        f"\n# Spaces backup credentials (added by dango deploy)\n"
        f"SPACES_ACCESS_KEY={config.spaces_access_key}\n"
        f"SPACES_SECRET_KEY={config.spaces_secret_key}\n"
        f"SPACES_BUCKET={bucket_name}\n"
        f"SPACES_REGION={config.region}\n"
    )
    # Append to existing .env
    result = ssh.exec_command("cat /srv/dango/project/.env 2>/dev/null || true")
    existing = result.stdout if result.stdout else ""
    ssh.write_remote_file("/srv/dango/project/.env", existing + env_lines, mode=0o600)
    ssh.exec_command("chown dango:dango /srv/dango/project/.env")

    # Write systemd timer/service files
    ssh.write_remote_file(
        "/etc/systemd/system/dango-backup.service",
        SYSTEMD_BACKUP_SERVICE,
        mode=0o644,
    )
    ssh.write_remote_file(
        "/etc/systemd/system/dango-backup.timer",
        SYSTEMD_BACKUP_TIMER,
        mode=0o644,
    )

    # Enable timer
    ssh.exec_command("systemctl daemon-reload")
    ssh.exec_command("systemctl enable --now dango-backup.timer")


def _build_spaces_config(
    config: WizardConfig,
    tracker: _ResourceTracker,
) -> dict[str, Any] | None:
    """Build SpacesConfig dict if backups are enabled."""
    if not config.enable_backups or not tracker.spaces_bucket:
        return None
    return {
        "bucket": tracker.spaces_bucket,
        "region": config.region,
    }


def _save_extra_metadata(
    project_root: Path,
    *,
    firewall_id: str,
    ssh_key_id: int,
    domain: str | None,
    spaces_config: dict[str, Any] | None,
) -> None:
    """Save additional metadata to cloud.yml beyond what save_provisioning_metadata writes."""
    from dango.config.loader import ConfigLoader
    from dango.config.models import SpacesConfig

    loader = ConfigLoader(project_root)
    existing = loader.load_cloud_config()
    if existing is None:
        return  # save_provisioning_metadata should have created it

    update: dict[str, Any] = {
        "firewall_id": firewall_id,
        "ssh_key_id": ssh_key_id,
    }
    if domain:
        update["domain"] = domain
    if spaces_config:
        update["spaces"] = SpacesConfig(**spaces_config)

    updated = existing.model_copy(update=update)
    loader.save_cloud_config(updated)


def _generate_cloud_profiles(
    ssh: Any,
    project_root: Path,
    size_slug: str | None = None,
) -> None:
    """Generate ``dbt/profiles.yml`` on the remote server tuned for its hardware.

    Args:
        ssh: Connected SSHManager.
        project_root: Local project root (to read dbt_project.yml).
        size_slug: DO size slug (e.g. ``"s-2vcpu-4gb"``).  If ``None``
            (BYOS), hardware is auto-detected via SSH.
    """
    import yaml

    from dango.platform.cloud.resize import generate_dbt_profiles_yml

    # Read project name from local dbt_project.yml
    dbt_project_path = project_root / "dbt" / "dbt_project.yml"
    project_name = "dango"
    if dbt_project_path.is_file():
        try:
            dbt_cfg = yaml.safe_load(dbt_project_path.read_text()) or {}
            project_name = dbt_cfg.get("name", "dango")
        except Exception:
            _logger.debug("dbt_project_yml_parse_failed", exc_info=True)

    if size_slug:
        # Known DO size — look up tier specs
        from dango.platform.cloud.provisioning import get_size_tier

        tier = get_size_tier(size_slug)
        vcpus = tier.vcpus if tier else 2
        ram_gb = tier.ram_gb if tier else 4
    else:
        # BYOS — auto-detect hardware via SSH
        nproc_result = ssh.exec_command("nproc")
        try:
            vcpus = int(nproc_result.stdout.strip())
        except (ValueError, AttributeError):
            vcpus = 2

        mem_result = ssh.exec_command("free -g | awk '/Mem:/{print $2}'")
        try:
            ram_gb = int(mem_result.stdout.strip())
        except (ValueError, AttributeError):
            ram_gb = 4

    content = generate_dbt_profiles_yml(project_name, vcpus, ram_gb)
    ssh.write_remote_file("/srv/dango/project/dbt/profiles.yml", content, mode=0o644)
    ssh.exec_command("chown dango:dango /srv/dango/project/dbt/profiles.yml")


def _start_services(ssh: Any) -> None:
    """Start dango-web (systemd) on remote server.

    Docker services (Metabase, dbt-docs) are started by the dango-web lifespan
    via DockerManager, which sets COMPOSE_PROJECT_NAME to ensure consistent
    container naming.  Do NOT run ``docker compose up`` directly here —
    that creates duplicate containers with a different project name.
    """
    ssh.exec_command("systemctl start dango-web", timeout=60)


def _wait_for_metabase(ssh: Any, timeout: int = 180) -> bool:
    """Poll Metabase health endpoint via SSH until it responds ok.

    Returns True if Metabase is ready, False if timeout.
    """
    attempts = timeout // 5
    for i in range(attempts):
        result = ssh.exec_command(
            "curl -sf http://localhost:3000/api/health 2>/dev/null | grep -q ok",
            timeout=10,
        )
        if result.exit_code == 0:
            return True
        if i % 6 == 0 and i > 0:
            console.print(f"    [dim]Still waiting... ({i * 5}s)[/dim]")
        time.sleep(5)
    _logger.warning("metabase_health_timeout", timeout=timeout)
    return False


def _health_check(url: str, timeout: int = 60, interval: int = 5) -> bool:
    """Poll server health endpoint until it responds 200.

    Returns:
        True if healthy, False if timeout.
    """
    import httpx

    attempts = timeout // interval
    for _ in range(attempts):
        try:
            resp = httpx.get(f"{url}/api/health", timeout=5, verify=False)
            if resp.status_code == 200:
                return True
        except Exception:
            _logger.debug("health_check_poll_error", url=url)
        time.sleep(interval)
    return False
