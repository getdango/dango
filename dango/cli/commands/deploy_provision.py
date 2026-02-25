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
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dango.cli import console
from dango.cli.commands.deploy_wizard import _EMAIL_RE, WizardConfig


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
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main provisioning function
# ---------------------------------------------------------------------------


def run_provisioning(
    project_root: Path,
    config: WizardConfig,
) -> ProvisionResult:
    """Execute the full provisioning pipeline (sub-steps 1-10).

    Args:
        project_root: Path to the local Dango project root.
        config: Wizard configuration from steps 1-9.

    Returns:
        ``ProvisionResult`` with deployment details.

    Raises:
        SystemExit: On unrecoverable failure (after cleanup).
    """
    from dango.platform.cloud.digitalocean import DigitalOceanClient
    from dango.platform.cloud.ssh import SSHManager

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
        ssh_key_id: int = key_data["ssh_key"]["id"]
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
        from dango.platform.cloud.server_setup import setup_server

        ssh.connect(droplet_ip, username="root")
        try:
            setup_result = setup_server(ssh, on_progress=_setup_progress, domain=config.domain)
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

        # --- Sub-step 7: Push secrets ---
        warnings: list[str] = []
        _status("Pushing secrets to server...")
        ssh.connect(droplet_ip, username="root")
        try:
            _push_secrets(ssh, project_root, warnings)
        finally:
            ssh.disconnect()

        # --- Sub-step 8: Create admin + enable auth ---
        _status("Creating admin account and enabling auth...")
        ssh.connect(droplet_ip, username="root")
        try:
            deploy_token = _create_admin_and_enable_auth(
                ssh, config.admin_email, config.admin_password
            )
        finally:
            ssh.disconnect()

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

        # --- Sub-step 10: Start services + health check + trigger initial sync ---
        _status("Starting services...")
        ssh.connect(droplet_ip, username="root")
        try:
            _start_services(ssh)
        finally:
            ssh.disconnect()

        url = f"https://{config.domain}" if config.domain else f"http://{droplet_ip}"
        health_ok = _health_check(url)
        if not health_ok:
            warnings.append(f"Health check failed. Server may still be starting. Try: {url}")

        # Trigger initial sync
        if not config.skip_initial_sync and health_ok:
            _trigger_initial_sync(url, deploy_token)

        return ProvisionResult(
            droplet_ip=droplet_ip,
            droplet_id=droplet_id,
            firewall_id=firewall_id,
            ssh_key_id=ssh_key_id,
            domain=config.domain,
            url=url,
            warnings=warnings,
        )

    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"\n[red]Provisioning failed:[/red] {exc}")
        console.print("Cleaning up resources...")
        errors = tracker.cleanup()
        if errors:
            console.print("[yellow]Cleanup errors:[/yellow]")
            for err in errors:
                console.print(f"  - {err}")
        else:
            console.print("[green]All resources cleaned up.[/green]")
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# Sub-step helpers
# ---------------------------------------------------------------------------


def _status(msg: str) -> None:
    """Print a provisioning status message."""
    console.print(f"\n[bold blue]>>>[/bold blue] {msg}")


def _setup_progress(step: str, status: str) -> None:
    """Progress callback for setup_server / sync_project_files."""
    if status == "done":
        console.print(f"    [green]Done:[/green] {step}")
    elif status == "skipped":
        console.print(f"    [dim]Skipped:[/dim] {step}")


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
    warnings: list[str],
) -> None:
    """Push .dlt/secrets.toml and .env to remote server."""
    # .dlt/secrets.toml
    secrets_path = project_root / ".dlt" / "secrets.toml"
    if secrets_path.exists():
        content = secrets_path.read_text()
        ssh.write_remote_file("/srv/dango/project/.dlt/secrets.toml", content, mode=0o600)
    else:
        warnings.append("No .dlt/secrets.toml found locally — skipped.")

    # .env
    env_path = project_root / ".env"
    if env_path.exists():
        content = env_path.read_text()
        ssh.write_remote_file("/srv/dango/project/.env", content, mode=0o600)

    # Fix ownership
    ssh.exec_command(
        "chown dango:dango "
        "/srv/dango/project/.dlt/secrets.toml "
        "/srv/dango/project/.env "
        "2>/dev/null; true"
    )


def _create_admin_and_enable_auth(
    ssh: Any,
    email: str,
    password: str,
) -> str:
    """Create admin user and enable auth on remote server.

    Returns:
        One-time deploy token for triggering initial sync.
    """
    # Validate email (shell injection prevention)
    if not _EMAIL_RE.match(email):
        raise ValueError(f"Invalid email format: {email}")

    # Hash password locally — never send plaintext to remote server
    from dango.auth.security import hash_password

    pw_hash = hash_password(password)

    # Generate one-time deploy token
    deploy_token = secrets.token_urlsafe(32)

    # Build Python script and base64-encode it (avoids shell injection)
    script = (
        "import sys, os, json\n"
        "sys.path.insert(0, '/srv/dango/project')\n"
        "os.chdir('/srv/dango/project')\n"
        "from pathlib import Path\n"
        "from dango.auth.database import create_user, init_db\n"
        "from dango.auth.models import Role, User\n"
        f"email = {email!r}\n"
        f"pw_hash = {pw_hash!r}\n"
        "db_path = Path('.dango/auth.db')\n"
        "db_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "init_db(db_path)\n"
        "user = User(email=email, password_hash=pw_hash, role=Role.ADMIN)\n"
        "try:\n"
        "    create_user(db_path, user)\n"
        "except Exception:\n"
        "    pass  # user may already exist\n"
        # Write deploy token
        f"token = {deploy_token!r}\n"
        "state_dir = Path('.dango/state')\n"
        "state_dir.mkdir(parents=True, exist_ok=True)\n"
        "(state_dir / 'deploy_token').write_text(token)\n"
    )
    encoded = base64.b64encode(script.encode()).decode()

    ssh.exec_command(
        f"sudo -u dango /srv/dango/venv/bin/python -c "
        f"\"import base64; exec(base64.b64decode('{encoded}'))\"",
        timeout=30,
    )

    # Enable auth
    ssh.write_remote_file("/srv/dango/project/.dango/auth.yml", "enabled: true\n", mode=0o644)
    ssh.exec_command("chown dango:dango /srv/dango/project/.dango/auth.yml")

    return deploy_token


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


def _start_services(ssh: Any) -> None:
    """Start Metabase (Docker) and dango-web (systemd) on remote server."""
    ssh.exec_command(
        "cd /srv/dango/project && sudo -u dango docker compose up -d",
        timeout=120,
    )
    ssh.exec_command("systemctl start dango-web", timeout=30)


def _health_check(url: str, timeout: int = 30, interval: int = 3) -> bool:
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
            pass
        time.sleep(interval)
    return False


def _trigger_initial_sync(url: str, deploy_token: str) -> None:
    """POST to /api/initial-sync/start to trigger background sync."""
    import httpx

    try:
        httpx.post(
            f"{url}/api/initial-sync/start",
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Authorization": f"Bearer {deploy_token}",
            },
            timeout=10,
            verify=False,
        )
    except Exception:
        pass  # Non-critical — user can trigger from dashboard
