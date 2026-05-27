"""dango/auth/permissions.py

Role-based access control with named permissions.

Maps each ``Role`` (Admin / Editor / Viewer) to a set of string-based
permission tokens.  Provides helper functions for permission checks and
a ``require_permission`` FastAPI dependency factory for route-level
enforcement.

Permission tokens use a ``<domain>.<action>`` naming convention
(e.g. ``source.sync``, ``users.manage``).  Admin uses a wildcard
(``"*"``) that grants all permissions automatically.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Request

from dango.auth.models import Role, User
from dango.exceptions import AuthenticationError, AuthorizationError

__all__ = [
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "check_permission",
    "get_permissions",
    "has_permission",
    "require_permission",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Permission registry — every valid permission token
# ---------------------------------------------------------------------------

# fmt: off
PERMISSIONS: frozenset[str] = frozenset({
    # Data sources
    "source.view",              # list sources, view status
    "source.view_credentials",  # view OAuth tokens / secrets
    "source.sync",              # trigger a sync
    "source.manage",            # add / remove / configure sources
    # CSV uploads
    "csv.upload",               # upload CSV files
    "csv.delete",               # delete uploaded CSVs
    # Transformation (dbt)
    "dbt.view",                 # view models, docs
    "dbt.run",                  # trigger dbt runs
    "dbt.manage",               # add / remove models
    # Visualization (Metabase)
    "dashboard.view",           # view dashboards
    "dashboard.create",         # create / edit dashboards
    "query.execute",            # run ad-hoc queries
    "dashboard.manage",         # manage Metabase settings
    # Platform
    "health.view",              # view health / status
    "logs.view",                # view logs
    "platform.manage",          # start / stop / configure platform
    "config.view",              # view project configuration
    "config.manage",            # modify project configuration
    # Auth & users
    "users.view",               # list users
    "users.manage",             # create / edit / deactivate users
    "auth.manage",              # manage auth settings (2FA policy, etc.)
    "audit.view",               # view audit logs
    # Future — notebooks (Phase 6)
    "notebooks.view",           # view notebooks
    "notebooks.execute",        # run notebook cells
    "notebooks.manage",         # create / delete notebooks
    # Future — governance (Phase 7)
    "governance.view",          # view PII reports
    "governance.manage",        # configure governance rules
    # Future — scheduler
    "scheduler.view",           # view scheduled jobs
    "scheduler.manage",         # create / edit schedules
})
# fmt: on

# ---------------------------------------------------------------------------
# Role → permission mapping
# ---------------------------------------------------------------------------

# fmt: off
ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN: frozenset({"*"}),
    Role.EDITOR: frozenset({
        "source.view", "source.sync", "source.manage",
        "csv.upload", "csv.delete",
        "dbt.view", "dbt.run", "dbt.manage",
        "dashboard.view", "dashboard.create", "query.execute",
        "health.view", "logs.view", "config.view",
        "notebooks.view", "notebooks.execute", "notebooks.manage",
        "scheduler.view",
        "governance.view",
    }),
    Role.VIEWER: frozenset({
        "source.view",
        "dbt.view",
        "dashboard.view",
        "health.view", "logs.view",
        "notebooks.view",
        "scheduler.view",
        "governance.view",
    }),
}
# fmt: on


# ---------------------------------------------------------------------------
# Permission check helpers
# ---------------------------------------------------------------------------


def get_permissions(role: Role) -> frozenset[str]:
    """Return the set of permissions granted to *role*.

    Returns an empty frozenset for unknown roles (defensive).
    """
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(user: User, permission: str) -> bool:
    """Check whether *user* holds *permission*.

    Returns ``False`` for inactive users (defense-in-depth) and for
    unknown permission tokens (with a logged warning).
    """
    if not user.is_active:
        return False

    if permission not in PERMISSIONS:
        logger.warning("Unknown permission checked: %s", permission)
        return False

    role_perms = get_permissions(user.role)
    if "*" in role_perms:
        return True

    return permission in role_perms


def check_permission(user: User, permission: str) -> None:
    """Raise :class:`~dango.exceptions.AuthorizationError` if *user* lacks *permission*."""
    if not has_permission(user, permission):
        raise AuthorizationError(
            f"Permission denied: {permission}",
            context={
                "permission": permission,
                "user_id": user.id,
                "role": user.role.value,
            },
        )


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------


def require_permission(permission: str) -> Callable[..., Any]:
    """Return an async FastAPI dependency that enforces *permission*.

    Usage::

        @router.post("/sources/{name}/sync")
        async def sync_source(
            name: str,
            user: User = Depends(require_permission("source.sync")),
        ):
            ...

    The dependency reads ``request.state.user`` (set by the auth
    middleware in TASK-015) and returns the authenticated ``User`` on
    success, so routes can use it directly.

    Raises:
        AuthenticationError: No user on the request (HTTP 401).
        AuthorizationError: User lacks the required permission (HTTP 403).
    """

    async def _dependency(request: Request) -> User:
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise AuthenticationError(
                "Authentication required",
                context={"permission": permission},
            )
        check_permission(user, permission)
        return user

    return _dependency
