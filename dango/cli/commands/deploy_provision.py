"""dango/cli/commands/deploy_provision.py

Provisioning orchestration for ``dango deploy`` — Step 10.

Orchestrates the full deployment pipeline: SSH key generation, droplet
provisioning, firewall, server setup, file sync, secrets push, admin
creation, optional backup setup, and service start with health check.

On failure, ``_ResourceTracker.cleanup()`` tears down any DO resources
that were created (no orphans).
"""

from __future__ import annotations

import re
import shlex
import ssl
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dango.cli import console
from dango.cli.commands.deploy_wizard import _EMAIL_RE, BYOSConfig, WizardConfig
from dango.exceptions import CloudAPIError, CloudProvisioningError
from dango.logging import get_logger
from dango.platform.cloud.project_files import (
    AS_DANGO,
    append_env_line_if_missing,
    read_project_file,
    write_project_file,
)
from dango.platform.cloud.ssh import CommandResult

_logger = get_logger(__name__)


def _sanitize_name(name: str) -> str:
    """Sanitize a path name for use in SSH keys, hostnames, and bucket names.

    Replaces non-alphanumeric characters with hyphens, collapses multiple
    hyphens, and strips leading/trailing hyphens. Returns 'dango-project'
    if the name is all non-alphanumeric (e.g., CJK-only directory names).
    """
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name.lower())
    sanitized = re.sub(r"-+", "-", sanitized)
    return sanitized.strip("-") or "dango-project"


@dataclass
class _ResourceTracker:
    """Tracks provisioned DO resources for cleanup on failure."""

    client: Any = None  # DigitalOceanClient
    droplet_id: int | None = None
    firewall_id: str | None = None
    ssh_key_id: int | None = None
    spaces_bucket: str | None = None
    spaces_created: bool = False  # this run created the bucket, so cleanup may delete it
    spaces_client: Any = None  # SpacesClient
    cloud_yml: Path | None = None  # restored after cleanup (see snapshot_cloud_yml)
    cloud_yml_before: bytes | None = None

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

        if self.spaces_bucket and self.spaces_created and self.spaces_client:
            try:
                self.spaces_client.delete_bucket()
            except Exception as exc:
                errors.append(f"Spaces bucket {self.spaces_bucket}: {exc}")

        self._restore_cloud_yml(errors)
        return errors

    def snapshot_cloud_yml(self, path: Path) -> None:
        """Remember cloud.yml as it is before this run writes to it."""
        self.cloud_yml = path
        self.cloud_yml_before = path.read_bytes() if path.exists() else None

    def _restore_cloud_yml(self, errors: list[str]) -> None:
        """Put cloud.yml back, so it never names resources the cleanup just deleted."""
        if self.cloud_yml is None or errors:
            return  # a failed delete: cloud.yml is the only record of what is still running
        try:
            if self.cloud_yml_before is None:
                self.cloud_yml.unlink(missing_ok=True)
            else:
                self.cloud_yml.write_bytes(self.cloud_yml_before)
        except OSError as exc:
            errors.append(f"{self.cloud_yml}: could not restore ({exc}); edit or delete it")


def _cleanup_and_report(tracker: _ResourceTracker) -> None:
    """Delete everything the failed or interrupted run created, naming each resource."""
    created = [
        f"{label} {value}"
        for label, value in (
            ("droplet", tracker.droplet_id),
            ("firewall", tracker.firewall_id),
            ("SSH key", tracker.ssh_key_id),
            ("Spaces bucket", tracker.spaces_bucket if tracker.spaces_created else None),
        )
        if value
    ]
    named = ", ".join(created)
    console.print(f"Cleaning up resources: {named or 'none created yet'}...")
    errors = tracker.cleanup()
    if errors:
        console.print("[yellow]Cleanup errors (delete these in the DigitalOcean console):[/yellow]")
        for err in errors:
            console.print(f"  - {err}")
        if tracker.cloud_yml is not None and tracker.cloud_yml.exists():
            console.print("Run [bold]dango deploy destroy[/bold] to finish the cleanup.")
        else:  # nothing recorded locally for destroy to read
            console.print("Delete the resources named above in the DigitalOcean console.")
    elif created:
        console.print("[green]All resources cleaned up.[/green]")


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
    # IPv4 CIDRs the web ports (80/443) were restricted to; empty = open to all
    allowed_ips: list[str] = field(default_factory=list)


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
    from dango.platform.cloud.size_availability import (
        SizeUnavailableError,
        check_size_in_region,
    )
    from dango.platform.cloud.ssh import SSHManager

    # BUG-251: Resolve project_root so Path(".").name is never empty
    project_root = project_root.resolve()

    tracker = _ResourceTracker()
    tracker.snapshot_cloud_yml(project_root / ".dango" / "cloud.yml")

    try:
        client = DigitalOceanClient()
        tracker.client = client

        # --- Sub-step 0: Verify the size exists in the region (creates nothing) ---
        if not check_size_in_region(config.region, config.size_slug):
            console.print(
                "[yellow]  Warning: could not verify size availability; "
                "DigitalOcean may reject this region.[/yellow]"
            )

        # --- Sub-step 1: Generate SSH key ---
        _status("Generating SSH key pair...")
        key_path = project_root / ".dango" / "cloud_key"
        ssh = SSHManager(key_path=key_path)
        public_key = ssh.generate_key_pair()

        # --- Sub-step 2: Upload SSH key to DO ---
        _status("Uploading SSH key to DigitalOcean...")
        key_name = f"dango-{_sanitize_name(project_root.name)}-{int(time.time())}"
        key_data = client.upload_ssh_key(key_name, public_key)
        ssh_key_id: int = key_data["id"]
        tracker.ssh_key_id = ssh_key_id

        # --- Sub-step 3: Provision droplet ---
        _status("Provisioning droplet (this takes ~60s)...")
        from dango.platform.cloud.provisioning import provision_droplet

        hostname = f"dango-{_sanitize_name(project_root.name)}"[:63]  # DO limit
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

        # Web access without a domain: explicit allowlist now, "detect" after SSH connects
        wants_detect = config.allowed_ips == ["detect"]
        applied_ips: list[str] = [] if wants_detect else list(config.allowed_ips)
        if applied_ips:
            from dango.platform.cloud.firewall import restrict_web_to_ips

            restrict_web_to_ips(client, firewall_id, applied_ips)
            console.print(f"  Web access restricted to {', '.join(applied_ips)}.")

        # --- Sub-step 5: Server setup ---
        _status("Setting up server (installs Docker, Caddy, Python, etc.)...")
        from dango.platform.cloud.server_setup import (
            resolve_install_source,
            setup_server,
        )

        install_source = resolve_install_source()
        ssh.connect(droplet_ip, username="root")
        try:
            deployer_ip = _deployer_ip(ssh) if config.allowed_ips else None
            if wants_detect:
                applied_ips = _restrict_to_detected_ip(client, firewall_id, deployer_ip)
            setup_result = setup_server(
                ssh,
                on_progress=_setup_progress,
                domain=config.domain,
                install_source=install_source,
            )
            if setup_result.warnings:
                for w in setup_result.warnings:
                    console.print(f"  [yellow]Warning:[/yellow] {w}")
            setup_notes = list(setup_result.warnings)
        finally:
            ssh.disconnect()

        # --- Sub-step 6: Sync project files ---
        _status("Syncing project files...")
        from dango.platform.cloud.file_sync import sync_project_files

        ssh.connect(droplet_ip, username="root")
        try:
            _print_port_notice(
                sync_project_files(
                    ssh,
                    project_root,
                    remote_host=droplet_ip,
                    on_progress=_setup_progress,
                )
            )
        finally:
            ssh.disconnect()

        # Fix ownership — file sync uploads as root, dbt/dango-web run as dango.
        # Pre-create dbt/target so Docker's bind mount doesn't recreate it as root.
        ssh.connect(droplet_ip, username="root")
        try:
            _fix_project_ownership(ssh)
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
        _carry_fallback_warnings(setup_notes, warnings)
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
            _prebuild_images(ssh, warnings)
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
        if applied_ips and not _ip_in_cidrs(deployer_ip, applied_ips):
            # The check runs from this machine, which the firewall does not allow.
            warnings.append(
                f"Health check skipped: web access is restricted to {', '.join(applied_ips)}. "
                "Check from an allowed address or run `dango remote status`."
            )
        else:
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
            allowed_ips=applied_ips,
        )

    except SizeUnavailableError as exc:
        # Nothing was created, so there is nothing to clean up.
        console.print(f"\n[red]Cannot deploy:[/red] {exc}")
        raise CloudProvisioningError(str(exc)) from exc
    except Exception as exc:
        # BUG-122: Handle empty exception messages (e.g. SSH timeout)
        err_msg = str(exc) or f"{type(exc).__name__} (no detail)"
        console.print(f"\n[red]Provisioning failed:[/red] {err_msg}")
        if isinstance(exc, AdminCreationError):
            console.print(
                "\n[dim]The admin-creation step failed on the server (details above)."
                "\nRun [bold]dango deploy[/bold] to retry with a fresh server.[/dim]"
            )
        elif isinstance(exc, CloudAPIError) and exc.is_permission_error:
            console.print(
                "\n[dim]Your DigitalOcean token is missing a permission. Required scopes: "
                "droplet (create, read, update, delete, actions), "
                "firewall (create, read, update, delete), "
                "ssh_key (create, read, delete), tag (create, read), "
                "regions/sizes/images (read). Spaces keys are separate.[/dim]"
            )
        elif isinstance(exc, CloudAPIError) and exc.status_code and 400 <= exc.status_code < 500:
            console.print("\n[dim]DigitalOcean rejected the request (see message above).[/dim]")
        else:
            console.print(
                "\n[dim]This is usually a transient infrastructure issue (not a Dango bug)."
                "\nRun [bold]dango deploy[/bold] to retry with a fresh server.[/dim]"
            )
        _cleanup_and_report(tracker)
        raise CloudProvisioningError(err_msg) from exc
    except BaseException:
        # Ctrl-C / SystemExit: the same cleanup, then the interruption propagates unchanged.
        console.print("\n[yellow]Interrupted.[/yellow]")
        _cleanup_and_report(tracker)
        raise


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
            _carry_fallback_warnings(setup_result.warnings, warnings)
        finally:
            ssh.disconnect()

        # --- Sub-step 2: Sync project files ---
        _status("Syncing project files...")
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _print_port_notice(
                sync_project_files(
                    ssh,
                    project_root,
                    remote_host=config.server_ip,
                    on_progress=_setup_progress,
                )
            )
        finally:
            ssh.disconnect()

        # Fix ownership — file sync uploads as root/ssh_user, dbt/dango-web run as dango.
        # Pre-create dbt/target so Docker's bind mount doesn't recreate it as root.
        ssh.connect(config.server_ip, username=config.ssh_user)
        try:
            _fix_project_ownership(ssh)
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
            _prebuild_images(ssh, warnings)
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


def _fix_project_ownership(ssh: Any) -> None:
    """Hand the synced project to ``dango`` and pre-create ``dbt/target`` as ``dango``.

    File sync uploads as root; dbt and dango-web run as dango.  ``-P`` (never
    follow symlinks) is chown's default with ``-R``; it is stated explicitly.
    ``dbt/target`` is pre-created so Docker's bind mount does not create it as root.
    """
    result = ssh.exec_command("chown -R -P dango:dango /srv/dango/project", timeout=30)
    if result.exit_code != 0:
        raise CloudProvisioningError(f"Failed to set file ownership: {result.stderr.strip()}")
    result = ssh.exec_command(f"{AS_DANGO} mkdir -p /srv/dango/project/dbt/target", timeout=10)
    if result.exit_code != 0:
        _logger.warning("mkdir_dbt_target_failed", stderr=result.stderr)


def _deployer_ip(ssh: Any) -> str | None:
    """Return the deployer's IPv4 as ``<ip>/32`` as the server sees it, or None.

    Reads ``$SSH_CLIENT`` over the open root session; no third-party lookup.
    """
    from dango.exceptions import CloudError
    from dango.platform.cloud.firewall import validate_ip_or_cidr

    result = ssh.exec_command("echo $SSH_CLIENT", timeout=10)
    fields = str(result.stdout or "").split()
    if not fields or "/" in fields[0]:
        return None
    try:
        return validate_ip_or_cidr(fields[0])
    except CloudError:
        return None


def _restrict_to_detected_ip(client: Any, firewall_id: str, deployer_ip: str | None) -> list[str]:
    """Apply the ``detect`` choice: restrict 80/443 to *deployer_ip* if known."""
    if deployer_ip is None:
        console.print(
            "  [yellow]Warning:[/yellow] Could not detect your IPv4 address. The server "
            "serves plain HTTP and its web ports are OPEN to the internet. Restrict them "
            "with: [bold]dango remote firewall allow-ip <your-ip>[/bold]"
        )
        return []
    from dango.platform.cloud.firewall import restrict_web_to_ips

    restrict_web_to_ips(client, firewall_id, [deployer_ip])
    console.print(
        f"  Web access restricted to {deployer_ip} (your address as the server sees it). "
        "Change it with [bold]dango remote firewall allow-ip[/bold]."
    )
    return [deployer_ip]


def _ip_in_cidrs(ip_cidr: str | None, cidrs: list[str]) -> bool:
    """Return True if the single address *ip_cidr* (``a.b.c.d/32``) is inside *cidrs*."""
    import ipaddress

    if ip_cidr is None:
        return False
    address = ipaddress.IPv4Address(ip_cidr.split("/")[0])
    return any(address in ipaddress.IPv4Network(c, strict=False) for c in cidrs)


def _prebuild_command(compose_proj: str) -> str:
    """Build the pre-deploy ``docker compose build`` command.

    The variable is passed through ``env`` *after* sudo: ``VAR=x sudo ...`` is dropped by
    sudo's env_reset, so Compose would fall back to the directory name and build twice.
    """
    return (
        "cd /srv/dango/project && sudo -u dango env "
        f"COMPOSE_PROJECT_NAME={shlex.quote(compose_proj)} docker compose build"
    )


def _carry_fallback_warnings(setup_warnings: list[str], warnings: list[str]) -> None:
    """Repeat the setup's loud compose-helper fallback warning at the end of the deploy."""
    from dango.platform.cloud.compose_migrate import FALLBACK_MARK

    warnings.extend(w for w in setup_warnings if w.startswith(FALLBACK_MARK))


def _prebuild_images(ssh: Any, warnings: list[str]) -> None:
    """Pre-build the Docker images: through the compose helper when it is installed, else legacy.

    A helper that cannot pin, render or build falls back to the docker group with a loud warning
    (added to *warnings*); an ordinary build failure raises.
    """
    from dango.platform.cloud.backup import get_remote_compose_project_name
    from dango.platform.cloud.compose_migrate import prebuild_images

    compose_proj = get_remote_compose_project_name(ssh, "/srv/dango/project")
    prebuild_images(ssh, warnings, legacy_command=_prebuild_command(compose_proj))


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
        if step == "detect_changes":
            console.print(
                "    [dim]Uploading files (Metabase driver ~80MB — may take several minutes on first deploy)...[/dim]"
            )
        console.print(f"    [dim][{ts}][/dim] [green]Done:[/green] {step}")
    elif status == "skipped":
        console.print(f"    [dim][{ts}] Skipped:[/dim] {step}")


def _print_port_notice(sync_result: Any) -> None:
    """Tell the user once when the uploaded project.yml got the standard server ports."""
    from dango.platform.cloud.server_ports import format_port_notice

    notice = format_port_notice(sync_result.port_changes)
    if notice:
        console.print(f"    [dim]{notice}[/dim]")


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

    # Written as the dango user (never through a symlink in the project).
    if secrets_path.exists():
        write_project_file(
            ssh, "/srv/dango/project/.dlt/secrets.toml", secrets_path.read_text(), mode=0o600
        )

    if env_path.exists():
        write_project_file(ssh, "/srv/dango/project/.env", env_path.read_text(), mode=0o600)


_ADMIN_SCRIPT_WAIT_SECONDS = 120
_ADMIN_SCRIPT_SSH_TIMEOUT = 180


class AdminCreationError(CloudProvisioningError):
    """The remote admin-creation step failed (message carries the real error)."""


def _build_admin_script(
    email: str,
    pw_hash: str,
    project_path: str = "/srv/dango/project",
    wait_seconds: int = _ADMIN_SCRIPT_WAIT_SECONDS,
) -> str:
    """Build the Python script that creates/updates the admin user on the server.

    ``systemctl start dango-web`` returns immediately, so on a first deploy the
    server may not have created/migrated ``auth.db`` yet.  The script waits for
    the ``users`` table and no pending auth migrations (polling every 2 s up to
    ``wait_seconds``), then falls
    back to ``apply_all_pending`` (idempotent) before creating the user.  Only
    APIs present in released Dango 1.0.12 are used because the server may run
    an older version than this client.

    Extracted for testability — validates via ``ast.parse()`` in unit tests.
    """
    return (
        "import sys, os, time, sqlite3\n"
        f"project_path = {project_path!r}\n"
        "sys.path.insert(0, project_path)\n"
        "os.chdir(project_path)\n"
        "from pathlib import Path\n"
        "from dango.auth.database import create_user, get_user_by_email, update_user\n"
        "from dango.auth.models import Role, User, UserUpdate\n"
        "from dango.exceptions import UserExistsError\n"
        "from dango.migrations import apply_all_pending, get_migrations_base_dir\n"
        "from dango.migrations.runner import MigrationRunner\n"
        f"email = {email!r}\n"
        f"pw_hash = {pw_hash!r}\n"
        f"wait_seconds = {int(wait_seconds)}\n"
        "db_path = Path('.dango/auth.db')\n"
        "def users_table_exists():\n"
        "    try:\n"
        "        conn = sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True)\n"
        "    except sqlite3.Error:\n"
        "        return False\n"
        "    try:\n"
        "        row = conn.execute(\n"
        "            \"SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'\"\n"
        "        ).fetchone()\n"
        "        return row is not None\n"
        "    except sqlite3.Error:\n"
        "        return False\n"
        "    finally:\n"
        "        conn.close()\n"
        "def auth_ready():\n"
        "    # users table present AND no pending auth migrations (each runs in its own\n"
        "    # transaction, so the table can appear before later columns do)\n"
        "    if not users_table_exists():\n"
        "        return False\n"
        "    try:\n"
        "        runner = MigrationRunner(db_path, 'auth', get_migrations_base_dir() / 'auth')\n"
        "        return runner.get_pending() == []\n"
        "    except Exception:\n"
        "        return False\n"
        "# The server migrates auth.db during startup; wait for it, else migrate ourselves\n"
        "deadline = time.monotonic() + wait_seconds\n"
        "while not auth_ready() and time.monotonic() < deadline:\n"
        "    time.sleep(min(2, max(0.0, deadline - time.monotonic())))\n"
        "if not auth_ready():\n"
        "    apply_all_pending(Path(project_path))\n"
        "user = User(email=email, password_hash=pw_hash, role=Role.ADMIN, must_change_password=True)\n"
        "try:\n"
        "    create_user(db_path, user)\n"
        "except UserExistsError:\n"
        "    existing = get_user_by_email(db_path, email)\n"
        "    if existing:\n"
        "        update_user(db_path, existing.id, UserUpdate(password_hash=pw_hash, email=email, must_change_password=True))\n"
    )


def _build_auth_timeout_script() -> str:
    """Build the Python script that sets cloud auth timeout config.

    Extracted for testability — validates via ``ast.parse()`` in unit tests.
    """
    return (
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


def _validate_admin_email(email: str) -> None:
    """Reject an admin email that could break out of a remote shell string or a ``.env`` line."""
    if "\n" in email or "'" in email or not _EMAIL_RE.fullmatch(email):
        raise ValueError(f"Invalid email format: {email!r}")


_SERVER_PYTHON = "/srv/dango/venv/bin/python"


def _run_remote_script(
    ssh: Any, script: str, *, purpose: str, timeout: int, python: str = _SERVER_PYTHON
) -> Any:
    """Upload *script* to a root-owned private directory, run it as ``dango``, then remove it.

    The server creates the directory (``mktemp -d``, ignoring root's ``TMPDIR``), so no
    path is predictable or plantable by the ``dango`` user; the directory is checked to
    be a real directory owned by root with mode 0700 before it is opened up to group
    ``dango`` (read-only), so root never writes into a location ``dango`` controls. The
    script may hold a password hash, hence no access for others. Removed even on failure.
    Any staging problem returns a failing result (the script is not run).
    """
    made = ssh.exec_command(f"env -u TMPDIR mktemp -d -t dango-{purpose}.XXXXXXXX", timeout=30)
    lines = (made.stdout or "").strip().splitlines()
    directory = lines[0].strip() if lines else ""
    # Only a directory shaped like the one asked for is ever removed.
    ours = re.fullmatch(rf"/[^\s]*/dango-{re.escape(purpose)}\.[A-Za-z0-9]{{8}}", directory)
    if made.exit_code != 0 or len(lines) != 1 or not ours:
        if ours:
            ssh.exec_command(f"rm -rf {shlex.quote(directory)}", timeout=30)
        if made.exit_code != 0:
            return made
        return CommandResult(
            stdout="",
            stderr=f"Could not stage script: unexpected mktemp output {(made.stdout or '')[:200]!r}",
            exit_code=1,
        )
    path = f"{directory}/{uuid.uuid4().hex}.py"
    q_dir, q_path = shlex.quote(directory), shlex.quote(path)
    try:
        for step in (
            f'test ! -L {q_dir} && [ "$(stat -c %u:%a {q_dir})" = 0:700 ]',
            f"chgrp dango {q_dir} && chmod 750 {q_dir}",
        ):
            prep = ssh.exec_command(step, timeout=30)
            if prep.exit_code != 0:
                return prep
        ssh.write_remote_file(path, script, mode=0o640)
        grant = ssh.exec_command(f"chgrp -h dango {q_path}", timeout=30)
        if grant.exit_code != 0:
            return grant
        return ssh.exec_command(f"sudo -u dango {python} {q_path}", timeout=timeout)
    finally:
        ssh.exec_command(f"rm -rf {q_dir}", timeout=30)


def _create_admin_and_enable_auth(
    ssh: Any,
    email: str,
    password: str,
) -> None:
    """Create admin user and enable auth on remote server."""
    _validate_admin_email(email)

    # Hash password locally — never send plaintext to remote server
    from dango.auth.security import hash_password

    pw_hash = hash_password(password)

    # Upload the script as a file instead of passing its text on a command line
    script = _build_admin_script(email, pw_hash)
    result = _run_remote_script(
        ssh,
        script,
        purpose="admin",
        timeout=_ADMIN_SCRIPT_SSH_TIMEOUT,
        python="/srv/dango/venv/bin/python3",
    )
    if result.exit_code != 0:
        raise AdminCreationError(
            f"Admin account creation failed:\n{result.stderr or result.stdout}"
        )

    # Persist admin email for systemd service (BUG-100), appended as the dango user
    result = append_env_line_if_missing(ssh, "/srv/dango/project/.env", "DANGO_ADMIN_EMAIL", email)
    if result.exit_code != 0:
        _logger.warning("persist_admin_email_failed", stderr=result.stderr)

    # Enable auth
    write_project_file(ssh, "/srv/dango/project/.dango/auth.yml", "enabled: true\n", mode=0o644)

    # Write cloud-specific auth timeouts (30-day session, 60-min idle)
    timeout_script = _build_auth_timeout_script()
    result = _run_remote_script(ssh, timeout_script, purpose="auth-timeout", timeout=15)
    if result.exit_code != 0:
        _logger.warning("auth_timeout_config_failed", stderr=result.stderr)

    # Ensure the entire .dango/ directory is owned by the dango user so it can
    # create files like dbt.lock in .dango/state/ at runtime.
    result = ssh.exec_command("chown -R -P dango:dango /srv/dango/project/.dango")
    if result.exit_code != 0:
        _logger.warning("chown_dango_dir_failed", stderr=result.stderr)


def _trigger_metabase_setup(ssh: Any, admin_email: str) -> None:
    """Trigger Metabase setup on the remote server.

    The deploy script sets ``DANGO_ADMIN_EMAIL`` in the server ``.env`` and
    restarts the ``dango-web`` service so that the lifespan
    ``setup_metabase_if_needed`` runs with the correct admin email already
    in the environment.
    """
    _validate_admin_email(admin_email)

    # Ensure admin email is in server .env for Metabase setup (appended as dango)
    result = append_env_line_if_missing(
        ssh, "/srv/dango/project/.env", "DANGO_ADMIN_EMAIL", admin_email
    )
    if result.exit_code != 0:
        _logger.warning("persist_metabase_email_failed", stderr=result.stderr)

    # Restart dango-web so lifespan re-runs setup_metabase_if_needed
    result = ssh.exec_command("systemctl restart dango-web", timeout=190)
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
        SERVER_DANGO_VERSION_COMMAND,
        SYSTEMD_BACKUP_TIMER,
        backup_service_unit,
    )
    from dango.platform.cloud.spaces import SpacesClient

    bucket_name = (
        config.spaces_bucket_name
        or f"dango-backup-{_sanitize_name(project_root.name)}-{config.region}"
    )
    spaces = SpacesClient(
        bucket=bucket_name,
        region=config.region,
        access_key=config.spaces_access_key,
        secret_key=config.spaces_secret_key,
    )
    tracker.spaces_client = spaces
    tracker.spaces_bucket = bucket_name

    # Create bucket (idempotent) — skip if reusing an existing bucket
    if not config.spaces_bucket_name:
        tracker.spaces_created = bool(spaces.create_bucket())
    else:
        console.print(f"  [dim]Using existing bucket '{bucket_name}'.[/dim]")

    # Write Spaces credentials to remote .env
    env_lines = (
        f"\n# Spaces backup credentials (added by dango deploy)\n"
        f"SPACES_ACCESS_KEY={config.spaces_access_key}\n"
        f"SPACES_SECRET_KEY={config.spaces_secret_key}\n"
        f"SPACES_BUCKET={bucket_name}\n"
        f"SPACES_REGION={config.region}\n"
    )
    # Append to existing .env (read and written as the dango user)
    existing = read_project_file(ssh, "/srv/dango/project/.env")
    write_project_file(ssh, "/srv/dango/project/.env", existing + env_lines, mode=0o600)

    # Write systemd timer/service files; the unit depends on the server's Dango
    # version (normally the version just installed, so the current unit)
    version = ssh.exec_command(SERVER_DANGO_VERSION_COMMAND, timeout=30)
    unit, unit_warning = backup_service_unit(
        version.stdout.strip() if version.exit_code == 0 else None
    )
    if unit_warning:
        console.print(f"  [yellow]Warning:[/yellow] {unit_warning}")
    ssh.write_remote_file("/etc/systemd/system/dango-backup.service", unit, mode=0o644)
    ssh.write_remote_file(
        "/etc/systemd/system/dango-backup.timer",
        SYSTEMD_BACKUP_TIMER,
        mode=0o644,
    )

    # Enable timer
    result = ssh.exec_command("systemctl daemon-reload")
    if result.exit_code != 0:
        _logger.warning("systemd_daemon_reload_failed", stderr=result.stderr)
    result = ssh.exec_command("systemctl enable --now dango-backup.timer")
    if result.exit_code != 0:
        _logger.warning("backup_timer_enable_failed", stderr=result.stderr)


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
    write_project_file(ssh, "/srv/dango/project/dbt/profiles.yml", content, mode=0o644)


def _start_services(ssh: Any) -> None:
    """Start dango-web (systemd) on remote server.

    Docker services (Metabase, dbt-docs) are started by the dango-web lifespan
    via DockerManager, which sets COMPOSE_PROJECT_NAME to ensure consistent
    container naming.  Do NOT run ``docker compose up`` directly here —
    that creates duplicate containers with a different project name.
    """
    result = ssh.exec_command("systemctl start dango-web", timeout=60)
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Failed to start dango-web service: {result.stderr.strip() or result.stdout.strip()}\n"
            "Check server logs with: dango remote logs"
        )


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

    is_https = url.startswith("https://")
    tls_notice_shown = False
    attempts = timeout // interval
    for _ in range(attempts):
        try:
            resp = httpx.get(f"{url}/api/health", timeout=5)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, ssl.SSLError):
            if is_https:
                _logger.info("health_check_https_not_ready", url=url)
                if not tls_notice_shown:
                    console.print(
                        "    [dim]HTTPS is not ready yet (certificate may still be "
                        "issuing); retry in a minute[/dim]"
                    )
                    tls_notice_shown = True
            else:
                _logger.debug("health_check_poll_error", url=url)
        except Exception:
            _logger.debug("health_check_poll_error", url=url)
        time.sleep(interval)
    return False
