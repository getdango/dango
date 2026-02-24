"""dango/cli/commands/serve.py

Production foreground server command for cloud deployments.

``dango serve`` runs the Dango web platform in the foreground (under
systemd on the server).  Unlike ``dango start`` it does not daemonise,
open a browser, or start the file watcher.

Reuses all startup helpers from ``dango.platform.common.startup``.
"""

from __future__ import annotations

import sys

import click


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Port (default: config value or 8800)")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int | None) -> None:
    """Run Dango in production server mode (foreground).

    Intended for use under systemd on a cloud server.  Runs all startup
    steps (migrations, Docker services, Metabase setup) then starts
    uvicorn in the foreground.

    Unlike ``dango start``, this command:

    \b
      - Binds to 0.0.0.0 (all interfaces)
      - Runs uvicorn in the foreground (no PID file)
      - Does not open a browser or start the file watcher
      - Minimal console output (no Rich formatting)
    """
    from dango.config import ConfigLoader
    from dango.platform.common.startup import (
        ensure_dbt_schemas,
        ensure_duckdb_driver,
        import_dashboards,
        run_pending_migrations,
        setup_metabase_if_needed,
        start_docker_services,
    )

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
    except (click.Abort, SystemExit):
        raise SystemExit(1) from None

    # Load config
    config_loader = ConfigLoader(project_root)
    config = config_loader.load_config()
    project_name = config.project.name
    organization = getattr(config.project, "organization", None)
    effective_port = port if port is not None else config.platform.port

    # 1. Migrations
    try:
        run_pending_migrations(project_root)
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # 2. dbt schemas
    try:
        ensure_dbt_schemas(project_root)
    except Exception as exc:
        print(f"Schema setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # 3. DuckDB driver
    try:
        ensure_duckdb_driver(project_root)
    except Exception as exc:
        print(f"DuckDB driver download failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # 4. Docker services
    try:
        start_docker_services(project_root)
    except Exception as exc:
        print(f"Docker services failed: {exc}", file=sys.stderr)
        _stop_docker_quiet(project_root)
        raise SystemExit(1) from exc

    # 5. Metabase setup
    try:
        setup_metabase_if_needed(project_root, project_name, organization)
    except Exception as exc:
        print(f"Metabase setup failed: {exc}", file=sys.stderr)
        _stop_docker_quiet(project_root)
        raise SystemExit(1) from exc

    # 6. Dashboard import (non-critical)
    try:
        import_dashboards(project_root)
    except Exception:
        pass

    # 7. Start uvicorn in foreground
    import uvicorn

    print(f"Starting Dango on {host}:{effective_port}")
    uvicorn.run("dango.web.app:app", host=host, port=effective_port, log_level="info")


def _stop_docker_quiet(project_root: object) -> None:
    """Best-effort Docker service cleanup."""
    try:
        from dango.platform import DockerManager

        DockerManager(project_root).stop_services()  # type: ignore[arg-type]
    except Exception:
        pass
