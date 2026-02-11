"""dango/cli/commands/web.py

Web UI backend server command.
"""

import click

from dango.cli import console


@click.command()
@click.option("--host", default="0.0.0.0", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload (development)")
@click.pass_context
def web(ctx: click.Context, host: str, port: int, reload: bool) -> None:
    """
    Start the Web UI backend server.

    This starts a FastAPI server that provides:
    - REST API for source management
    - Real-time WebSocket updates
    - Service health monitoring

    The API documentation is available at:
      http://localhost:8080/api/docs

    Examples:
      dango web                      # Start on default port 8080
      dango web --port 3001         # Start on custom port
      dango web --reload            # Start with auto-reload (dev mode)
    """
    from ..utils import require_project_context

    console.print("\n🍡 [bold]Starting Dango Web UI[/bold]\n")

    try:
        project_root = require_project_context(ctx)

        console.print(f"[dim]Project root: {project_root}[/dim]")
        console.print(f"[dim]Server: http://{host}:{port}[/dim]")
        console.print(f"[dim]API docs: http://{host}:{port}/api/docs[/dim]")
        console.print()

        # Import and run uvicorn
        import uvicorn

        from dango.web import app as web_app

        # Set project root in app state
        web_app.app.state.project_root = project_root

        # Start server
        console.print("[green]Starting server...[/green]")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        uvicorn.run(web_app.app, host=host, port=port, reload=reload, log_level="info")

    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Error starting server:[/red] {e}")
        import traceback

        console.print(traceback.format_exc())
        raise click.Abort() from e
