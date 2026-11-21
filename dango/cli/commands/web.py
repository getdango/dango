"""dango/cli/commands/web.py

Web UI backend server command.
"""

import os
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from dango.cli import console


@click.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload (development)")
@click.pass_context
def web(ctx: click.Context, host: str, port: int, reload: bool) -> None:
    """
    Start the Web UI backend server only.

    This starts a FastAPI server that provides:
    - REST API for source management
    - Real-time WebSocket updates
    - Service health monitoring

    Before serving it runs the same local startup checks as "dango serve": it loads the
    project .env file, checks the DuckDB version, rotates logs, applies pending database
    migrations and creates the dbt schemas.

    It does NOT start Docker, Metabase, dbt-docs or the file watcher, download the Metabase
    driver, or open a browser. "dango start" is the normal way to run Dango on your own
    computer; "dango serve" is for servers. Binds to 0.0.0.0 (all interfaces) unless --host
    is given.

    The API documentation is available at:
      http://localhost:8080/api/docs

    Examples:
      dango web                      # Start on default port 8080
      dango web --port 3001         # Start on custom port
      dango web --reload            # Start with auto-reload (dev mode)
    """
    from ..utils import require_project_context

    console.print("\n🍡 [bold]Starting Dango Web UI[/bold]\n")

    project_root: Path | None = None
    previous_root = os.environ.get("DANGO_PROJECT_ROOT")
    try:
        project_root = require_project_context(ctx)

        console.print(f"[dim]Project root: {escape(str(project_root))}[/dim]")
        console.print(f"[dim]Server: http://{host}:{port}[/dim]")
        console.print(f"[dim]API docs: http://{host}:{port}/api/docs[/dim]")
        console.print()

        from dango.platform.common.startup_prologue import (
            PrologueError,
            describe_env_load_error,
            load_project_env,
            run_local_prologue,
        )
        from dango.utils.error_text import rich_error_text

        # Audit logging and other helpers resolve the project from this variable, not from
        # the working directory (which may be a subdirectory of the project).
        os.environ["DANGO_PROJECT_ROOT"] = str(project_root)
        try:
            load_project_env(project_root)
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            reason, hint = describe_env_load_error(exc, "web")
            console.print(
                f"[red]Cannot read {escape(str(project_root / '.env'))}:[/red] {escape(reason)}"
            )
            if hint:
                console.print(f"[dim]{escape(hint)}[/dim]")
            raise click.Abort() from exc

        def _waiting(holder: str) -> None:
            console.print(
                f"[yellow]Waiting for another Dango process to finish with the warehouse "
                f"({escape(holder)}). Press Ctrl+C to cancel.[/yellow]"
            )

        def _note(message: str) -> None:
            console.print(f"[yellow]{escape(message)}[/yellow]")

        try:
            applied = run_local_prologue(
                project_root, lock_schemas=True, on_lock_wait=_waiting, on_note=_note
            )
        except PrologueError as err:
            console.print(
                f"[red]Error starting server:[/red] {err.label}: "
                f"{rich_error_text(project_root, err.cause)}"
            )
            raise click.Abort() from err.cause
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]")
            raise click.Abort() from None
        for db_name, migrations in applied.items():
            if migrations:
                console.print(f"[dim]Applied {len(migrations)} migration(s) to {db_name}[/dim]")

        # Import and run uvicorn
        import uvicorn

        target: Any
        if reload:
            # The reloader starts the app in a new process: uvicorn needs an import string, and
            # the new process finds the project through DANGO_PROJECT_ROOT (set above).
            target = "dango.web.app:app"
        else:
            from dango.web import app as web_app

            # Set project root in app state
            web_app.app.state.project_root = project_root
            target = web_app.app

        # Start server
        console.print("[green]Starting server...[/green]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        uvicorn.run(target, host=host, port=port, reload=reload, log_level="info")

    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped[/yellow]")
    except click.Abort:
        raise
    except Exception as e:
        from dango.utils.error_text import rich_error_text

        console.print(f"\n[red]Error starting server:[/red] {rich_error_text(project_root, e)}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(escape(traceback.format_exc()))
        raise click.Abort() from e
    finally:
        # The server has stopped: leave no project pointer behind for code in this process.
        if previous_root is None:
            os.environ.pop("DANGO_PROJECT_ROOT", None)
        else:
            os.environ["DANGO_PROJECT_ROOT"] = previous_root
