"""dango/cli/commands/auth.py

CLI subcommands for auth toggle, user management, audit log, and recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from dango.cli import console


@click.group()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Manage user authentication and access."""


def _get_db_path(ctx: click.Context) -> tuple[Path, Path]:
    """Return (project_root, db_path) or abort if auth.db missing."""
    from dango.auth.admin import get_auth_db_path
    from dango.cli.utils import print_error, require_project_context

    project_root = require_project_context(ctx)
    db_path = get_auth_db_path(project_root)
    if not db_path.exists():
        print_error("Auth database not found. Run 'dango start' or 'dango migrate run' first.")
        raise click.Abort()
    return project_root, db_path


def _handle_error(exc: Exception) -> None:
    """Print error and show traceback in debug mode."""
    console.print(f"[red]Error:[/red] {exc}")
    from dango.exceptions import is_debug_mode

    if is_debug_mode():
        import traceback

        console.print(traceback.format_exc())


@auth.command("enable")
@click.pass_context
def auth_enable(ctx: click.Context) -> None:
    """Enable authentication for this project."""
    from dango.auth.admin import (
        ensure_admin,
        format_credentials_panel,
        get_auth_db_path,
        is_auth_enabled,
        set_auth_enabled,
    )
    from dango.cli.utils import print_info, print_success, require_project_context

    try:
        project_root = require_project_context(ctx)
        if is_auth_enabled(project_root):
            print_info("Authentication is already enabled.")
            return

        # Create admin before enabling so a failure doesn't leave auth
        # enabled with no admin user.
        db_path = get_auth_db_path(project_root)
        if db_path.exists():
            import re

            email_re = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
            while True:
                email = click.prompt("Admin email")
                if email_re.match(email):
                    break
                console.print("[red]Invalid email format.[/red]")
            result = ensure_admin(db_path, email=email)
            if result is not None:
                user, password = result
                console.print(format_credentials_panel(user.email, password))
            else:
                print_info("Admin account already exists.")
        set_auth_enabled(project_root, enabled=True)
        print_success("Authentication enabled.")
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("disable")
@click.pass_context
def auth_disable(ctx: click.Context) -> None:
    """Disable authentication for this project."""
    from dango.auth.admin import is_auth_enabled, set_auth_enabled
    from dango.cli.utils import confirm, print_info, print_warning, require_project_context

    try:
        project_root = require_project_context(ctx)
        if not is_auth_enabled(project_root):
            print_info("Authentication is already disabled.")
            return
        if not confirm("Disabling auth removes all access control. Continue?"):
            return
        set_auth_enabled(project_root, enabled=False)
        print_warning("Authentication disabled. All endpoints are now public.")
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("add-user")
@click.argument("email")
@click.option("--role", type=click.Choice(["admin", "editor", "viewer"]), default="viewer")
@click.option(
    "--password",
    "use_password",
    is_flag=True,
    help="Generate a temporary password instead of an invite link.",
)
@click.option("--base-url", default="http://localhost:8800", help="Base URL for invite links.")
@click.pass_context
def auth_add_user(
    ctx: click.Context, email: str, role: str, use_password: bool, base_url: str
) -> None:
    """Create a new user with an invite link (or --password for a temp password)."""
    from dango.auth.database import create_user
    from dango.auth.models import Role, User
    from dango.exceptions import UserExistsError

    try:
        project_root, db_path = _get_db_path(ctx)

        if use_password:
            from dango.auth.admin import format_credentials_panel
            from dango.auth.security import generate_temp_password, hash_password

            password = generate_temp_password()
            user = User(
                email=email,
                password_hash=hash_password(password),
                role=Role(role),
                must_change_password=True,
            )
            create_user(db_path, user)
            console.print(format_credentials_panel(user.email, password, title="User created"))
        else:
            from datetime import timedelta

            from dango.auth.security import generate_invite_token

            raw_token, token_hash = generate_invite_token()
            user = User(
                email=email,
                password_hash=None,
                role=Role(role),
                invite_token_hash=token_hash,
                invite_expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
            )
            create_user(db_path, user)
            invite_url = f"{base_url.rstrip('/')}/invite/{raw_token}"
            console.print(f"\n[green]User created:[/green] {user.email}")
            console.print(f"[bold]Invite link:[/bold] {invite_url}")
            console.print("[dim]Link expires in 72 hours. Share it securely.[/dim]\n")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.USER_CREATED,
            email=email,
            user_id=user.id,
            details={"via": "cli", "role": role},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except UserExistsError:
        from dango.cli.utils import print_error

        print_error(f"A user with email '{email.strip().lower()}' already exists.")
        raise click.Abort() from None
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("list-users")
@click.pass_context
def auth_list_users(ctx: click.Context) -> None:
    """List all users."""
    from rich.table import Table

    from dango.auth.database import list_users

    try:
        _, db_path = _get_db_path(ctx)
        users = list_users(db_path)
        if not users:
            console.print("[dim]No users found.[/dim]")
            console.print("[dim]Use 'dango auth add-user <email>' to create one.[/dim]")
            return

        now = datetime.now(timezone.utc)
        table = Table(title=f"Users ({len(users)})", show_header=True)
        table.add_column("Email", style="cyan")
        table.add_column("Role", style="blue")
        table.add_column("Status")
        table.add_column("Last Login", style="dim")
        table.add_column("Created", style="dim")
        for user in users:
            if not user.is_active:
                status = "[red]Inactive[/red]"
            elif user.locked_until is not None and user.locked_until > now:
                status = "[yellow]Locked[/yellow]"
            elif user.invite_expires_at is not None and user.invite_expires_at > now:
                status = "[magenta]Invited[/magenta]"
            elif (
                user.invite_expires_at is not None
                and user.invite_expires_at <= now
                and user.last_login is None
            ):
                status = "[yellow]Invite Expired[/yellow]"
            else:
                status = "[green]Active[/green]"
            last_login = user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "Never"
            table.add_row(
                user.email,
                user.role.value,
                status,
                last_login,
                user.created_at.strftime("%Y-%m-%d"),
            )
        console.print(table)
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("reset-password")
@click.argument("email")
@click.pass_context
def auth_reset_password(ctx: click.Context, email: str) -> None:
    """Generate a new temporary password for a user."""
    from dango.auth.admin import format_credentials_panel
    from dango.auth.database import get_user_by_email, invalidate_all_user_sessions, update_user
    from dango.auth.models import UserUpdate
    from dango.auth.security import generate_temp_password, hash_password
    from dango.cli.utils import print_error

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()

        password = generate_temp_password()
        update_user(
            db_path,
            user.id,
            UserUpdate(password_hash=hash_password(password), must_change_password=True),
        )
        invalidate_all_user_sessions(db_path, user.id)
        console.print(format_credentials_panel(user.email, password, title="Password reset"))
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.PASSWORD_RESET,
            email=user.email,
            user_id=user.id,
            details={"via": "cli"},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("deactivate-user")
@click.argument("email")
@click.pass_context
def auth_deactivate_user(ctx: click.Context, email: str) -> None:
    """Deactivate a user account (soft disable)."""
    from dango.auth.database import (
        deactivate_user,
        get_user_by_email,
        invalidate_all_user_sessions,
        list_users,
    )
    from dango.auth.models import Role
    from dango.cli.utils import print_error, print_success

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()
        if user.role == Role.ADMIN:
            active_admins = [
                u for u in list_users(db_path, active_only=True) if u.role == Role.ADMIN
            ]
            if len(active_admins) <= 1:
                print_error("Cannot deactivate the only active admin.")
                raise click.Abort()
        deactivate_user(db_path, user.id)
        invalidate_all_user_sessions(db_path, user.id)
        print_success(f"User '{user.email}' deactivated.")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.USER_DEACTIVATED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli"},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("reactivate-user")
@click.argument("email")
@click.pass_context
def auth_reactivate_user(ctx: click.Context, email: str) -> None:
    """Reactivate a deactivated user account."""
    from dango.auth.database import get_user_by_email, update_user
    from dango.auth.models import UserUpdate
    from dango.cli.utils import print_error, print_info, print_success

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()
        if user.is_active:
            print_info(f"User '{user.email}' is already active.")
            return
        update_user(db_path, user.id, UserUpdate(is_active=True))
        print_success(f"User '{user.email}' reactivated.")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.USER_REACTIVATED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli"},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("delete-user")
@click.argument("email")
@click.pass_context
def auth_delete_user(ctx: click.Context, email: str) -> None:
    """Permanently delete a user (cannot be undone)."""
    from dango.auth.database import delete_user, get_user_by_email, list_users
    from dango.auth.models import Role
    from dango.cli.utils import confirm, print_error, print_success, print_warning

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()
        if user.role == Role.ADMIN:
            active_admins = [
                u for u in list_users(db_path, active_only=True) if u.role == Role.ADMIN
            ]
            if len(active_admins) <= 1:
                print_error("Cannot delete the only active admin.")
                raise click.Abort()
        if not confirm(f"Permanently delete user '{user.email}'? This cannot be undone."):
            return
        try:
            from dango.auth.metabase_sync import _load_metabase_credentials, delete_metabase_user

            creds = _load_metabase_credentials(project_root)
            if creds and creds.get("metabase_url"):
                delete_metabase_user(db_path, user.id, project_root, creds["metabase_url"])
        except Exception:
            print_warning("Metabase cleanup failed (user will still be deleted).")
        delete_user(db_path, user.id)
        print_success(f"User '{user.email}' deleted.")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.USER_DELETED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli"},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """Show authentication status."""
    from rich.panel import Panel

    from dango.auth.admin import get_auth_db_path, is_auth_enabled
    from dango.cli.utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        enabled = is_auth_enabled(project_root)
        lines: list[str] = []
        if enabled:
            lines.append("[green]Authentication:[/green] Enabled")
        else:
            lines.append("[yellow]Authentication:[/yellow] Disabled")

        db_path = get_auth_db_path(project_root)
        if db_path.exists():
            from dango.auth.database import list_users
            from dango.auth.models import Role

            now = datetime.now(timezone.utc)
            users = list_users(db_path)
            active = [u for u in users if u.is_active]
            admins = [u for u in active if u.role == Role.ADMIN]
            editors = [u for u in active if u.role == Role.EDITOR]
            viewers = [u for u in active if u.role == Role.VIEWER]
            locked = [u for u in users if u.locked_until is not None and u.locked_until > now]
            inactive = [u for u in users if not u.is_active]
            lines.append(f"[bold]Users:[/bold] {len(active)} active")
            lines.append(
                f"  Admins: {len(admins)}  Editors: {len(editors)}  Viewers: {len(viewers)}"
            )
            if inactive:
                lines.append(f"  [dim]Inactive: {len(inactive)}[/dim]")
            if locked:
                lines.append(f"  [yellow]Locked: {len(locked)}[/yellow]")
        else:
            lines.append("[dim]Auth database not initialized.[/dim]")
            lines.append("[dim]Run 'dango start' or 'dango migrate run' first.[/dim]")
        console.print(Panel("\n".join(lines), title="Auth Status", border_style="blue"))
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("unlock")
@click.argument("email")
@click.pass_context
def auth_unlock(ctx: click.Context, email: str) -> None:
    """Unlock a locked-out user account."""
    from dango.auth.database import get_user_by_email, update_user
    from dango.auth.models import UserUpdate
    from dango.cli.utils import print_error, print_info, print_success

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()
        if user.locked_until is None and user.failed_login_attempts == 0:
            print_info(f"User '{user.email}' is not locked.")
            return
        update_user(
            db_path,
            user.id,
            UserUpdate(failed_login_attempts=0, locked_until=None),
        )
        # Also clear IP-based lockouts so the user can log in from any IP
        from dango.auth.lockout import unlock_account

        unlock_account(db_path, user.email)
        print_success(f"User '{user.email}' unlocked.")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.ACCOUNT_UNLOCKED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli"},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("change-role")
@click.argument("email")
@click.argument("role", type=click.Choice(["admin", "editor", "viewer"]))
@click.pass_context
def auth_change_role(ctx: click.Context, email: str, role: str) -> None:
    """Change a user's role (admin, editor, viewer)."""
    from dango.auth.database import get_user_by_email, list_users, update_user
    from dango.auth.models import Role, UserUpdate
    from dango.cli.utils import print_error, print_info, print_success, print_warning

    try:
        project_root, db_path = _get_db_path(ctx)
        user = get_user_by_email(db_path, email)
        if user is None:
            print_error(f"User '{email}' not found.")
            raise click.Abort()
        if user.role == Role(role):
            print_info(f"User '{user.email}' already has role '{role}'.")
            return
        old_role = user.role.value
        if user.role == Role.ADMIN:
            active_admins = [
                u for u in list_users(db_path, active_only=True) if u.role == Role.ADMIN
            ]
            if len(active_admins) <= 1:
                print_error("Cannot demote the only active admin.")
                raise click.Abort()
        update_user(db_path, user.id, UserUpdate(role=Role(role)))
        try:
            from dango.auth.metabase_sync import _load_metabase_credentials, sync_user_role

            creds = _load_metabase_credentials(project_root)
            if creds and creds.get("metabase_url"):
                sync_user_role(db_path, user.id, project_root, creds["metabase_url"])
        except Exception:
            print_warning("Metabase role sync failed (local role still updated).")
        print_success(f"User '{user.email}' role changed to {role}.")
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.ROLE_CHANGED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli", "old_role": old_role, "new_role": role},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("audit")
@click.option("--since", type=str, default=None, help="Filter events after date (YYYY-MM-DD).")
@click.option("--type", "event_type", type=str, default=None, help="Filter by event type.")
@click.option("--limit", type=int, default=50, help="Max events to show.")
@click.pass_context
def auth_audit(
    ctx: click.Context,
    since: str | None,
    event_type: str | None,
    limit: int,
) -> None:
    """Query the authentication audit log."""
    from rich.table import Table

    from dango.auth.audit import AuditEvent, query_audit_log
    from dango.cli.utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        log_dir = project_root / ".dango" / "logs"
        audit_event: AuditEvent | None = None
        if event_type is not None:
            try:
                audit_event = AuditEvent(event_type)
            except ValueError:
                valid = ", ".join(e.value for e in AuditEvent)
                _handle_error(ValueError(f"Unknown event type '{event_type}'. Valid: {valid}"))
                raise click.Abort() from None
        entries = query_audit_log(
            since=since,
            event_type=audit_event,
            limit=limit,
            log_dir=log_dir,
        )
        if not entries:
            console.print("[dim]No audit events found.[/dim]")
            return
        table = Table(title=f"Audit Log ({len(entries)} events)", show_header=True)
        table.add_column("Timestamp", style="dim")
        table.add_column("Event", style="cyan")
        table.add_column("Email", style="blue")
        table.add_column("Details")
        for entry in entries:
            d = entry.get("details") or {}
            table.add_row(
                entry.get("timestamp", "")[:19],
                entry.get("event", ""),
                entry.get("email", "—"),
                ", ".join(f"{k}={v}" for k, v in d.items()) or "—",
            )
        console.print(table)
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None


@auth.command("recover")
@click.pass_context
def auth_recover(ctx: click.Context) -> None:
    """Create a recovery admin account (emergency use)."""
    from dango.auth.admin import format_credentials_panel
    from dango.auth.database import create_user
    from dango.auth.models import Role, User
    from dango.auth.security import generate_temp_password, hash_password
    from dango.cli.utils import print_warning
    from dango.exceptions import UserExistsError

    try:
        project_root, db_path = _get_db_path(ctx)
        email = click.prompt("Recovery admin email", default="recovery@localhost")
        password = generate_temp_password()
        user = User(
            email=email,
            password_hash=hash_password(password),
            role=Role.ADMIN,
            must_change_password=True,
        )
        try:
            create_user(db_path, user)
        except UserExistsError:
            from dango.cli.utils import print_error

            print_error(f"A user with email '{email.strip().lower()}' already exists.")
            raise click.Abort() from None
        print_warning("Recovery admin created. Delete this account after regaining access.")
        console.print(format_credentials_panel(user.email, password, title="Recovery admin"))
        from dango.auth.audit import AuditEvent, log_auth_event

        log_auth_event(
            event_type=AuditEvent.USER_CREATED,
            email=user.email,
            user_id=user.id,
            details={"via": "cli", "role": "admin", "recovery": True},
            log_dir=project_root / ".dango" / "logs",
        )
    except click.Abort:
        raise
    except Exception as exc:
        _handle_error(exc)
        raise click.Abort() from None
