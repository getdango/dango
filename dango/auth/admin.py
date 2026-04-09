"""dango/auth/admin.py

Business logic for admin management and auth configuration.

Provides helpers for reading/writing the auth toggle in ``.dango/auth.yml``,
creating the first admin user on startup, and formatting credential output
for the terminal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.panel import Panel

from dango.auth.database import create_user, list_users
from dango.auth.models import Role, User
from dango.auth.security import generate_temp_password, hash_password
from dango.config.loader import ConfigLoader

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_auth_db_path(project_root: Path) -> Path:
    """Return path to ``.dango/auth.db``."""
    return project_root / ".dango" / "auth.db"


def get_auth_config_path(project_root: Path) -> Path:
    """Return path to ``.dango/auth.yml``."""
    return project_root / ".dango" / "auth.yml"


# ---------------------------------------------------------------------------
# Auth toggle (reads/writes .dango/auth.yml)
# ---------------------------------------------------------------------------


def is_auth_enabled(project_root: Path) -> bool:
    """Read enabled flag from ``.dango/auth.yml``.

    Returns ``False`` if the file is missing or has no ``enabled`` key.
    """
    config_path = get_auth_config_path(project_root)
    if not config_path.exists():
        return False
    loader = ConfigLoader(project_root)
    data: dict[str, Any] = loader.load_yaml(config_path)
    return bool(data.get("enabled", False))


def set_auth_enabled(project_root: Path, *, enabled: bool) -> None:
    """Write enabled flag to ``.dango/auth.yml``.

    Preserves any other keys already present in the file.
    """
    config_path = get_auth_config_path(project_root)
    loader = ConfigLoader(project_root)

    data: dict[str, Any] = {}
    if config_path.exists():
        data = loader.load_yaml(config_path)

    data["enabled"] = enabled
    loader.save_yaml(data, config_path)


# ---------------------------------------------------------------------------
# Admin bootstrapping
# ---------------------------------------------------------------------------


def ensure_admin(
    db_path: Path,
    email: str = "admin@dango.local",
) -> tuple[User, str] | None:
    """Create an admin user if none exists.

    Returns ``(user, raw_password)`` if a new admin was created, or
    ``None`` if an admin already exists.
    """
    users = list_users(db_path, active_only=True)
    admins = [u for u in users if u.role == Role.ADMIN]
    if admins:
        return None

    password = generate_temp_password()
    user = User(
        email=email,
        password_hash=hash_password(password),
        role=Role.ADMIN,
        must_change_password=True,
    )
    create_user(db_path, user)
    return user, password


# ---------------------------------------------------------------------------
# Credential display
# ---------------------------------------------------------------------------


def format_credentials_panel(
    email: str,
    password: str,
    *,
    title: str = "Admin account created",
) -> Panel:
    """Return a Rich ``Panel`` displaying credentials.

    The panel is formatted for terminal output with clear instructions
    to save the temporary password.
    """
    content = (
        f"[bold]Email:[/bold]    {email}\n"
        f"[bold]Password:[/bold] {password}\n"
        "\n"
        "[dim]Save this password — it will not be shown again.[/dim]\n"
        "[dim]The user must change it on first login.[/dim]"
    )
    return Panel(content, title=f"[green]{title}[/green]", border_style="green")
