"""dango/cli/commands/deploy_wizard.py

Interactive deployment wizard and non-interactive validation for ``dango deploy``.

Steps 1-8 gather DigitalOcean configuration (region, size, admin
credentials, etc.) before handing off to ``deploy_provision.py``.

Also provides a BYOS (Bring Your Own Server) wizard path that skips
cloud provisioning and goes straight to server setup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from dango.cli import console

# ---------------------------------------------------------------------------
# BUG-252: Non-interactive confirm helper (now delegated to cli/utils.py)
# ---------------------------------------------------------------------------


def _safe_confirm(prompt: str, default: bool = True, *, non_interactive: bool = False) -> bool:
    """Thin wrapper around :func:`dango.cli.utils.safe_confirm`.

    When *non_interactive* is ``True``, returns *default* without prompting.
    """
    if non_interactive:
        return default
    from dango.cli.utils import safe_confirm

    return safe_confirm(prompt, default=default)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


@dataclass
class WizardConfig:
    """Configuration collected from the deployment wizard."""

    region: str
    size_slug: str
    size_tier: Any | None  # DropletSizeTier or None if custom slug
    domain: str | None
    admin_email: str
    admin_password: str
    skip_oauth: bool
    enable_backups: bool
    monthly_cost: int
    push_secrets: bool = True
    # Backup credentials (only set if enable_backups is True)
    spaces_access_key: str | None = None
    spaces_secret_key: str | None = None


@dataclass
class BYOSConfig:
    """Configuration collected from the BYOS (Bring Your Own Server) wizard."""

    server_ip: str
    ssh_user: str
    ssh_key_path: str
    domain: str | None
    admin_email: str
    admin_password: str
    skip_oauth: bool
    push_secrets: bool = True


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------


def _step_prereqs(project_root: Path) -> None:
    """Step 1: Check deployment prerequisites.

    Raises:
        SystemExit: If a prerequisite is missing.
    """
    # Check DIGITALOCEAN_TOKEN — env var > project credential > user credential > prompt
    from dango.config.cloud_credentials import get_do_token, save_do_token

    # Env var takes highest priority — no confirmation needed
    token_from_env = bool(os.environ.get("DIGITALOCEAN_TOKEN"))
    token = get_do_token(project_root=project_root)
    if token and not token_from_env:
        # BUG-238a: Show token suffix and allow changing (stored credential)
        masked = f"...{token[-4:]}"
        keep = _safe_confirm(f"  Keep DigitalOcean token ending in {masked}?", default=True)
        if not keep:
            new_token = click.prompt("  Enter new DigitalOcean API token", hide_input=True)
            if new_token.strip():
                token = new_token.strip()
                save_do_token(token, project_root=project_root)
    elif token and token_from_env:
        console.print("  [dim]Using DigitalOcean token from environment variable.[/dim]")
    else:
        console.print("[yellow]DigitalOcean API token not found.[/yellow]")
        console.print(
            "\n  Create an API token at: "
            "[link=https://cloud.digitalocean.com/account/api/tokens]"
            "https://cloud.digitalocean.com/account/api/tokens[/link]\n"
        )
        token = click.prompt("Enter your DigitalOcean API token", hide_input=True)
        if not token.strip():
            raise SystemExit(1)
        token = token.strip()
        # BUG-127: Persist token so subsequent commands don't re-prompt
        save_do_token(token, project_root=project_root)

    # BUG-238c: Validate token upfront with lightweight API call
    import httpx

    try:
        resp = httpx.get(
            "https://api.digitalocean.com/v2/account",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if resp.status_code == 401:
            console.print("[red]Token authentication failed.[/red]")
            console.print(
                "  Generate a new token at: https://cloud.digitalocean.com/account/api/tokens"
            )
            raise SystemExit(1)
        resp.raise_for_status()
        console.print("[green]  \u2713 DigitalOcean token validated[/green]")
    except (httpx.ConnectError, httpx.TimeoutException):
        console.print(
            "[yellow]  Warning: Could not reach DigitalOcean API "
            "(network error). Proceeding...[/yellow]"
        )
    except httpx.HTTPError:
        console.print(
            "[yellow]  Warning: Could not validate token "
            "(unexpected API response). Proceeding...[/yellow]"
        )

    os.environ["DIGITALOCEAN_TOKEN"] = token

    # Check project has sources
    sources_yml = project_root / ".dango" / "sources.yml"
    if not sources_yml.exists():
        console.print(
            "[red]Error:[/red] No sources.yml found. "
            "Run [bold]dango source add[/bold] to configure data sources first."
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Region selection
# ---------------------------------------------------------------------------


def _step_region() -> str:
    """Step 2: Select deployment region.

    Returns:
        DO region slug.
    """
    from dango.platform.cloud.provisioning import list_regions, suggest_nearest_region

    suggested = suggest_nearest_region()
    regions = list_regions()

    console.print("\n[bold]Step 2: Select Region[/bold]")
    console.print(
        f"  Suggested (nearest): [green]{suggested.name}[/green] ({suggested.slug})"
        f" [dim]— nearest match based on your UTC offset[/dim]\n"
    )

    for i, r in enumerate(regions, 1):
        gdpr_tag = " [dim](GDPR)[/dim]" if r.gdpr else ""
        console.print(f"  {i:2d}. {r.name:<22s} ({r.slug}){gdpr_tag}")

    console.print()
    choice = click.prompt(
        "  Region number or slug",
        default=suggested.slug,
        show_default=True,
    )

    # Accept number or slug
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(regions):
            return regions[idx].slug
        console.print(f"[yellow]Invalid number, using default: {suggested.slug}[/yellow]")
        return suggested.slug

    # Validate slug
    valid_slugs = {r.slug for r in regions}
    if choice in valid_slugs:
        return str(choice)

    console.print(f"[yellow]Unknown region '{choice}', using default: {suggested.slug}[/yellow]")
    return suggested.slug


# ---------------------------------------------------------------------------
# Size selection
# ---------------------------------------------------------------------------


def _step_size() -> tuple[str, Any | None]:
    """Step 3: Select droplet size.

    Returns:
        Tuple of (size_slug, DropletSizeTier or None).
    """
    from dango.platform.cloud.provisioning import (
        DEFAULT_TIER,
        SIZE_TIERS,
        get_size_tier,
        validate_custom_size,
    )

    console.print("\n[bold]Step 3: Select Server Size[/bold]\n")

    for i, tier in enumerate(SIZE_TIERS, 1):
        default_marker = " (recommended)" if tier == DEFAULT_TIER else ""
        warning = f"  [yellow]{tier.warning}[/yellow]" if tier.warning else ""
        console.print(
            f"  {i}. {tier.name:<13s} {tier.vcpus} vCPU / {tier.ram_gb}GB RAM / "
            f"{tier.disk_gb}GB SSD — ${tier.price_monthly}/mo{default_marker}{warning}"
        )

    console.print(f"  {len(SIZE_TIERS) + 1}. Custom slug")
    console.print("        [dim]See https://slugs.do-api.dev/ — minimum 4GB RAM required.[/dim]")
    console.print()

    choice = click.prompt("  Size number", default="1", show_default=True)

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(SIZE_TIERS):
            tier = SIZE_TIERS[idx]
            return tier.slug, tier
        if idx == len(SIZE_TIERS):
            # Custom slug
            while True:
                slug = click.prompt("  Enter DO size slug (e.g. s-2vcpu-4gb)")
                if validate_custom_size(slug):
                    return slug, get_size_tier(slug)
                console.print(
                    "  [red]Invalid slug format.[/red] Expected format: s-{vcpus}vcpu-{ram}gb"
                )
                if not _safe_confirm("  Try again?", default=True):
                    console.print(f"  Using default: {DEFAULT_TIER.name}")
                    return DEFAULT_TIER.slug, DEFAULT_TIER

    # Fallback to default
    console.print(f"[yellow]Invalid choice, using default: {DEFAULT_TIER.name}[/yellow]")
    return DEFAULT_TIER.slug, DEFAULT_TIER


# ---------------------------------------------------------------------------
# Admin credentials
# ---------------------------------------------------------------------------


def _step_admin() -> tuple[str, str]:
    """Step 4: Admin email and password.

    Returns:
        Tuple of (email, password).
    """
    from dango.auth.security import check_password_strength

    console.print("\n[bold]Step 4: Admin Account[/bold]")
    console.print("  Create the first admin user for your deployment.\n")

    # Email (with confirmation — BUG-237)
    while True:
        email = click.prompt("  Admin email")
        if not _EMAIL_RE.match(email):
            console.print("  [red]Invalid email format.[/red]")
            continue
        confirm = click.prompt("  Confirm admin email")
        if confirm == email:
            break
        console.print("  [red]Emails do not match. Please try again.[/red]")

    # Password (from env or auto-generated)
    env_password = os.environ.get("DANGO_ADMIN_PASSWORD")
    if env_password:
        issues = check_password_strength(env_password, email=email)
        if issues:
            console.print(f"  [red]DANGO_ADMIN_PASSWORD is weak:[/red] {'; '.join(issues)}")
            raise SystemExit(1)
        console.print("  [dim]Using password from DANGO_ADMIN_PASSWORD env var.[/dim]")
        return email, env_password

    import secrets

    password = secrets.token_urlsafe(16)
    console.print("  [dim]Admin password will be shown at the end of deployment.[/dim]")

    return email, password


# ---------------------------------------------------------------------------
# Sources check
# ---------------------------------------------------------------------------


def _step_sources(project_root: Path) -> list[dict[str, str]]:
    """Step 5: List configured sources and check credentials.

    Returns:
        List of source dicts with 'name' and 'type' keys.
    """
    from dango.config.helpers import load_config

    console.print("\n[bold]Step 5: Data Sources[/bold]\n")

    config = load_config(project_root)
    sources: list[dict[str, str]] = []

    for src in config.sources.sources:
        sources.append({"name": src.name, "type": src.type.value})
        console.print(f"  - {src.name} ({src.type.value})")

    if not sources:
        console.print("  [yellow]No data sources configured.[/yellow]")
        console.print(
            "  You can add sources after deployment with [bold]dango source add[/bold].\n"
        )

    # Check for .dlt/secrets.toml
    secrets_path = project_root / ".dlt" / "secrets.toml"
    if secrets_path.exists():
        console.print(f"\n  [green]Found[/green] {secrets_path.relative_to(project_root)}")
    elif sources:
        console.print(
            "\n  [yellow]Warning:[/yellow] No .dlt/secrets.toml found. "
            "Source credentials may be missing."
        )

    return sources


def _step_secrets(project_root: Path) -> bool:
    """Step 5b: Confirm secrets push.

    Returns True if user wants to push secrets to the server.
    """
    secrets_path = project_root / ".dlt" / "secrets.toml"
    env_path = project_root / ".env"

    files: list[str] = []
    if secrets_path.exists():
        files.append(".dlt/secrets.toml")
    if env_path.exists():
        files.append(".env")

    if not files:
        return False

    console.print("\n  [bold]Secrets to push to server:[/bold]")
    for f in files:
        console.print(f"    - {f}")

    return _safe_confirm("  Push these secrets during deployment?", default=True)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _step_oauth() -> bool:
    """Step 6: Skip OAuth setup (always skipped during deploy wizard).

    Returns:
        True (OAuth is always skipped — configured post-deployment).
    """
    console.print("\n[bold]Step 6: OAuth Sources[/bold]")
    console.print("  OAuth tokens will be configured after deployment.")
    console.print("  Run [cyan]dango oauth setup[/cyan] on the server.\n")

    return True


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def _step_backups() -> tuple[bool, str | None, str | None]:
    """Step 7: Enable automated backups?

    Returns:
        Tuple of (enable_backups, access_key, secret_key).
    """
    console.print("\n[bold]Step 7: Automated Backups[/bold]")
    console.print("  Daily backups at 2:00 AM UTC to DigitalOcean Spaces ($5/mo for 250GB).")
    console.print("  Backs up: DuckDB warehouse, auth database, config files, dlt credentials.")
    console.print(
        "  Keeps 14 most recent local backups (2 weeks). Spaces backups retained until deleted.\n"
    )

    enable = _safe_confirm("  Enable automated backups?", default=True)
    if not enable:
        return False, None, None

    # Offer to enter keys now or skip
    import inquirer
    from inquirer import themes

    answers = inquirer.prompt(
        [
            inquirer.List(
                "backup_action",
                message="Spaces credentials",
                choices=[
                    "Enter Spaces keys now",
                    "Skip — configure backups later",
                ],
                carousel=True,
            )
        ],
        theme=themes.GreenPassion(),
    )
    if not answers or answers["backup_action"].startswith("Skip"):
        console.print("  [yellow]Backups enabled but keys not configured.[/yellow]")
        console.print("  [dim]Run 'dango remote backup enable' later to add Spaces keys.[/dim]")
        return True, None, None

    console.print(
        "\n  Create Spaces access keys at: "
        "[link=https://cloud.digitalocean.com/spaces]"
        "https://cloud.digitalocean.com/spaces[/link]"
        " (Access Keys tab)\n"
    )

    access_key = click.prompt("  Spaces access key")
    secret_key = click.prompt("  Spaces secret key", hide_input=True)

    # Validate keys by trying to construct client
    try:
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(
            bucket="dango-validate-test",
            region="nyc3",
            access_key=access_key,
            secret_key=secret_key,
        )
        # Try a lightweight operation to confirm auth
        client.list_objects(prefix="__nonexistent__")
        console.print("  [green]Spaces credentials verified.[/green]")
    except Exception:
        console.print(
            "  [yellow]Warning:[/yellow] Could not verify Spaces credentials. "
            "Backups may fail if keys are incorrect."
        )

    return True, access_key, secret_key


# ---------------------------------------------------------------------------
# Cost summary
# ---------------------------------------------------------------------------


def _get_monthly_cost(size_slug: str, enable_backups: bool) -> int:
    """Calculate estimated monthly cost."""
    from dango.platform.cloud.provisioning import get_size_tier

    tier = get_size_tier(size_slug)
    droplet_cost = tier.price_monthly if tier else 24  # default estimate
    spaces_cost = 5 if enable_backups else 0
    return droplet_cost + spaces_cost


def _step_cost_summary(
    region: str,
    size_slug: str,
    enable_backups: bool,
) -> int:
    """Step 8: Show cost summary and confirm.

    Returns:
        Monthly cost in USD.

    Raises:
        SystemExit: If user declines.
    """
    from dango.platform.cloud.provisioning import get_region_info, get_size_tier

    console.print("\n[bold]Step 8: Cost Summary[/bold]\n")

    tier = get_size_tier(size_slug)
    region_info = get_region_info(region)
    droplet_cost = tier.price_monthly if tier else 24

    region_display = f"{region_info.name} ({region})" if region_info else region
    size_display = f"{tier.name} ({size_slug})" if tier else size_slug

    console.print(f"  Region:    {region_display}")
    console.print(f"  Size:      {size_display} — ${droplet_cost}/mo")
    if enable_backups:
        console.print("  Backups:   Enabled — ~$5/mo")

    total = _get_monthly_cost(size_slug, enable_backups)
    console.print(f"\n  [bold]Estimated total: ${total}/mo[/bold]")
    console.print(
        "  [dim]Costs are billed directly by DigitalOcean to your account,"
        " not through Dango.[/dim]\n"
    )

    if not _safe_confirm("  Proceed with deployment?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        raise SystemExit(0)

    return total


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_wizard(project_root: Path) -> WizardConfig:
    """Run the interactive deployment wizard (steps 1-8).

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Completed ``WizardConfig`` ready for provisioning.
    """
    console.print("\n[bold blue]Dango Cloud Deployment Wizard[/bold blue]\n")

    # Step 1: Prerequisites
    _step_prereqs(project_root)
    console.print("  [green]Prerequisites OK.[/green]")

    # Step 2: Region
    region = _step_region()

    # Step 3: Size
    size_slug, size_tier = _step_size()

    # Step 4: Admin credentials
    admin_email, admin_password = _step_admin()

    # Step 5: Sources + Secrets
    _step_sources(project_root)
    push_secrets = _step_secrets(project_root)

    # Step 6: OAuth
    skip_oauth = _step_oauth()

    # Step 7: Backups
    enable_backups, spaces_access_key, spaces_secret_key = _step_backups()

    # Step 8: Cost summary + confirm
    monthly_cost = _step_cost_summary(region, size_slug, enable_backups)

    return WizardConfig(
        region=region,
        size_slug=size_slug,
        size_tier=size_tier,
        domain=None,
        admin_email=admin_email,
        admin_password=admin_password,
        skip_oauth=skip_oauth,
        enable_backups=enable_backups,
        monthly_cost=monthly_cost,
        push_secrets=push_secrets,
        spaces_access_key=spaces_access_key,
        spaces_secret_key=spaces_secret_key,
    )


def run_non_interactive(
    project_root: Path,
    *,
    region: str | None = None,
    size: str | None = None,
    domain: str | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
    skip_backups: bool = False,
) -> WizardConfig:
    """Validate CLI flags for non-interactive deployment.

    Args:
        project_root: Path to the Dango project root.
        region: DO region slug. Defaults to nearest.
        size: Droplet size slug. Defaults to standard tier.
        domain: Optional custom domain.
        admin_email: Required admin email.
        admin_password: Required admin password (or from DANGO_ADMIN_PASSWORD env).
        skip_backups: Skip backup setup.

    Returns:
        Validated ``WizardConfig``.

    Raises:
        SystemExit: If required params are missing or invalid.
    """
    from dango.auth.security import check_password_strength
    from dango.platform.cloud.provisioning import (
        DEFAULT_TIER,
        get_size_tier,
        suggest_nearest_region,
        validate_custom_size,
    )

    # Prerequisites
    _step_prereqs(project_root)

    # Region
    if region is None:
        region = suggest_nearest_region().slug
    else:
        from dango.platform.cloud.provisioning import get_region_info

        if get_region_info(region) is None:
            console.print(f"[red]Error:[/red] Unknown region '{region}'.")
            raise SystemExit(1)

    # Size
    if size is None:
        size = DEFAULT_TIER.slug
    else:
        tier = get_size_tier(size)
        if tier is None and not validate_custom_size(size):
            console.print(f"[red]Error:[/red] Invalid size slug '{size}'.")
            raise SystemExit(1)

    # Admin email
    if not admin_email:
        console.print("[red]Error:[/red] --admin-email is required for --non-interactive.")
        raise SystemExit(1)
    if not _EMAIL_RE.match(admin_email):
        console.print(f"[red]Error:[/red] Invalid email format: {admin_email}")
        raise SystemExit(1)

    # Admin password
    if not admin_password:
        admin_password = os.environ.get("DANGO_ADMIN_PASSWORD")
    if not admin_password:
        console.print(
            "[red]Error:[/red] --admin-password or DANGO_ADMIN_PASSWORD env required "
            "for --non-interactive."
        )
        raise SystemExit(1)
    issues = check_password_strength(admin_password, email=admin_email)
    if issues:
        console.print(f"[red]Error:[/red] Weak password: {'; '.join(issues)}")
        raise SystemExit(1)

    size_tier = get_size_tier(size)
    monthly_cost = _get_monthly_cost(size, not skip_backups)

    return WizardConfig(
        region=region,
        size_slug=size,
        size_tier=size_tier,
        domain=domain,
        admin_email=admin_email,
        admin_password=admin_password,
        skip_oauth=True,
        enable_backups=not skip_backups,
        monthly_cost=monthly_cost,
    )


# ---------------------------------------------------------------------------
# BYOS (Bring Your Own Server) wizard
# ---------------------------------------------------------------------------


def _step_byos_server(project_root: Path) -> tuple[str, str, str]:
    """Prompt for server IP, SSH user, and SSH key path.

    Returns:
        Tuple of (server_ip, ssh_user, ssh_key_path).
    """
    console.print("\n[bold]Step 1: Server Connection[/bold]\n")

    server_ip = click.prompt("  Server IP or hostname")

    console.print(
        "  [dim]Common SSH users: root (DO/Hetzner), ubuntu (AWS), your username (GCP)[/dim]"
    )
    ssh_user = click.prompt("  SSH user", default="root", show_default=True)

    console.print("\n  SSH key options:")
    console.print("    1. Use an existing SSH key")
    console.print("    2. Generate a new key pair\n")

    key_choice = click.prompt("  Choice", default="1", show_default=True)

    if key_choice == "2":
        from dango.platform.cloud.ssh import SSHManager

        key_path = project_root / ".dango" / "cloud_key"
        ssh = SSHManager(key_path=key_path)
        public_key = ssh.generate_key_pair()

        console.print(f"\n  [green]Generated key pair at:[/green] {key_path}")
        console.print("\n  [bold]Public key (add to server's ~/.ssh/authorized_keys):[/bold]")
        console.print(f"  {public_key}\n")
        click.prompt(
            "  Press Enter once the key is added to your server", default="", show_default=False
        )
        return server_ip, ssh_user, str(key_path)

    # Existing key
    default_key = str(Path.home() / ".ssh" / "id_ed25519")
    key_path_str = click.prompt("  Path to SSH private key", default=default_key, show_default=True)
    key_path = Path(key_path_str).expanduser()

    if not key_path.exists():
        console.print(f"  [red]Error:[/red] SSH key not found: {key_path}")
        raise SystemExit(1)

    return server_ip, ssh_user, str(key_path)


def _validate_ssh_connectivity(
    server_ip: str,
    ssh_user: str,
    ssh_key_path: str,
) -> None:
    """Test SSH connectivity and warn if not Ubuntu.

    Raises:
        SystemExit: If SSH connection fails.
    """
    from dango.platform.cloud.ssh import SSHManager

    console.print("\n  Testing SSH connection...")
    ssh = SSHManager(key_path=Path(ssh_key_path))
    try:
        ssh.connect(server_ip, username=ssh_user)
    except Exception as exc:
        console.print(f"  [red]Error:[/red] SSH connection failed: {exc}")
        console.print(
            "\n  Troubleshooting:"
            "\n  - Verify the server IP and SSH user are correct"
            "\n  - Ensure your SSH key is in the server's authorized_keys"
            "\n  - Check that port 22 is open on the server"
        )
        raise SystemExit(1) from exc

    try:
        # Check if Ubuntu
        result = ssh.exec_command("cat /etc/os-release 2>/dev/null")
        if result.success and "ubuntu" not in result.stdout.lower():
            console.print(
                "  [yellow]Warning:[/yellow] Server does not appear to be running Ubuntu. "
                "Dango is tested on Ubuntu 22.04+. Proceed with caution."
            )
        else:
            console.print("  [green]SSH connection successful.[/green]")
    finally:
        ssh.disconnect()


def run_byos_wizard(project_root: Path) -> BYOSConfig:
    """Run the BYOS interactive wizard.

    Args:
        project_root: Path to the Dango project root.

    Returns:
        Completed ``BYOSConfig`` ready for server setup.
    """
    console.print("\n[bold blue]Dango BYOS Deployment Wizard[/bold blue]")
    console.print("[dim]Deploy to your own server (any cloud provider)[/dim]\n")

    # Check sources.yml exists (skip DO token check)
    sources_yml = project_root / ".dango" / "sources.yml"
    if not sources_yml.exists():
        console.print(
            "[red]Error:[/red] No sources.yml found. "
            "Run [bold]dango source add[/bold] to configure data sources first."
        )
        raise SystemExit(1)
    console.print("  [green]Prerequisites OK.[/green]")

    # Step 1: Server connection
    server_ip, ssh_user, ssh_key_path = _step_byos_server(project_root)

    # Step 2: Validate SSH
    _validate_ssh_connectivity(server_ip, ssh_user, ssh_key_path)

    # Step 3: Admin credentials
    admin_email, admin_password = _step_admin()

    # Step 4: Sources + Secrets
    _step_sources(project_root)
    push_secrets = _step_secrets(project_root)

    # Step 5: OAuth
    skip_oauth = _step_oauth()

    # Confirmation
    console.print("\n[bold]Deployment Summary[/bold]\n")
    console.print(f"  Server:    {server_ip} (SSH user: {ssh_user})")
    console.print(f"  SSH key:   {ssh_key_path}")
    console.print(f"  Admin:     {admin_email}")
    console.print()

    if not _safe_confirm("  Proceed with deployment?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        raise SystemExit(0)

    return BYOSConfig(
        server_ip=server_ip,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path,
        domain=None,
        admin_email=admin_email,
        admin_password=admin_password,
        skip_oauth=skip_oauth,
        push_secrets=push_secrets,
    )


def run_byos_non_interactive(
    project_root: Path,
    *,
    server_ip: str | None = None,
    ssh_user: str = "root",
    ssh_key_path: str | None = None,
    domain: str | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
) -> BYOSConfig:
    """Validate params for non-interactive BYOS deployment.

    Args:
        project_root: Path to the Dango project root.
        server_ip: Required server IP or hostname.
        ssh_user: SSH user. Defaults to root.
        ssh_key_path: Path to SSH key. Defaults to .dango/cloud_key.
        domain: Optional custom domain.
        admin_email: Required admin email.
        admin_password: Required admin password (or from DANGO_ADMIN_PASSWORD env).

    Returns:
        Validated ``BYOSConfig``.

    Raises:
        SystemExit: If required params are missing or invalid.
    """
    from dango.auth.security import check_password_strength

    # Check sources.yml exists
    sources_yml = project_root / ".dango" / "sources.yml"
    if not sources_yml.exists():
        console.print(
            "[red]Error:[/red] No sources.yml found. "
            "Run [bold]dango source add[/bold] to configure data sources first."
        )
        raise SystemExit(1)

    # Server IP
    if not server_ip:
        console.print("[red]Error:[/red] --server-ip is required for --byos.")
        raise SystemExit(1)

    # SSH key path
    if ssh_key_path is None:
        ssh_key_path = str(project_root / ".dango" / "cloud_key")
    if not Path(ssh_key_path).expanduser().exists():
        console.print(f"[red]Error:[/red] SSH key not found: {ssh_key_path}")
        raise SystemExit(1)

    # Admin email
    if not admin_email:
        console.print("[red]Error:[/red] --admin-email is required for --byos.")
        raise SystemExit(1)
    if not _EMAIL_RE.match(admin_email):
        console.print(f"[red]Error:[/red] Invalid email format: {admin_email}")
        raise SystemExit(1)

    # Admin password
    if not admin_password:
        admin_password = os.environ.get("DANGO_ADMIN_PASSWORD")
    if not admin_password:
        console.print(
            "[red]Error:[/red] --admin-password or DANGO_ADMIN_PASSWORD env required for --byos."
        )
        raise SystemExit(1)
    issues = check_password_strength(admin_password, email=admin_email)
    if issues:
        console.print(f"[red]Error:[/red] Weak password: {'; '.join(issues)}")
        raise SystemExit(1)

    # Validate SSH connectivity
    _validate_ssh_connectivity(server_ip, ssh_user, ssh_key_path)

    return BYOSConfig(
        server_ip=server_ip,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path,
        domain=domain,
        admin_email=admin_email,
        admin_password=admin_password,
        skip_oauth=True,
    )
