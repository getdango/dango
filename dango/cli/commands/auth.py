"""dango/cli/commands/auth.py

User authentication and access management commands (Phase 2).

OAuth credential management has moved to ``dango oauth``.
"""

import click


@click.group()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """Manage user authentication and access.

    Commands for login, logout, user management, and role assignment
    will be added in Phase 2. For OAuth credential management, use
    ``dango oauth``.
    """
    pass
