"""dango/cli/commands/serve.py

Production foreground server command for cloud deployments.

``dango serve`` runs the Dango web platform in the foreground (under
systemd on the server).  Unlike ``dango start`` it does not daemonise,
open a browser, or start the file watcher.

Reuses all startup helpers from ``dango.platform.common.startup``.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
        rotate_logs,
        run_pending_migrations,
        setup_metabase_if_needed,
        start_docker_services,
    )

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
    except (click.Abort, SystemExit):
        raise SystemExit(1) from None

    # Load .env so DANGO_ADMIN_EMAIL and other env vars are available under systemd (BUG-100)
    from dotenv import load_dotenv

    load_dotenv(project_root / ".env")

    # Load config (M6: catch config errors cleanly)
    try:
        config_loader = ConfigLoader(project_root)
        config = config_loader.load_config()
    except Exception as exc:
        print(f"Failed to load project config: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    project_name = config.project.name
    organization = getattr(config.project, "organization", None)
    effective_port = port if port is not None else config.platform.port

    # 0. Log rotation (never-fail)
    rotate_logs(project_root)

    # 1. Migrations
    try:
        run_pending_migrations(project_root)
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # BUG-104: Stop leftover containers before DuckDB write operations.
    # Containers from a previous crashed run may hold the DuckDB file lock.
    _stop_docker_quiet(project_root)

    # 2. dbt schemas
    try:
        ensure_dbt_schemas(project_root)
    except Exception as exc:
        print(f"Schema setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # 3. DuckDB driver — non-fatal if JAR already exists (BUG-124: synced from local)
    try:
        ensure_duckdb_driver(project_root)
    except Exception as exc:
        driver_jar = project_root / "metabase-plugins" / "duckdb.metabase-driver.jar"
        if driver_jar.is_file():
            print(
                f"WARNING: DuckDB driver download failed ({exc}), "
                "but driver JAR exists (synced from local). Continuing.",
                file=sys.stderr,
            )
        else:
            print(f"DuckDB driver download failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

    # 4. Docker services
    try:
        start_docker_services(project_root)
    except Exception as exc:
        print(f"Docker services failed: {exc}", file=sys.stderr)
        _stop_docker_quiet(project_root)
        raise SystemExit(1) from exc

    # 5. Metabase setup (non-fatal — BUG-103: prevents systemd crash loop)
    # setup_metabase_if_needed returns a dict even on failure — inspect the result.
    try:
        setup_result = setup_metabase_if_needed(project_root, project_name, organization)
        if not setup_result.get("success") and not setup_result.get("skipped"):
            errors = setup_result.get("errors") or []
            error_str = "; ".join(str(e) for e in errors) if errors else "unknown"
            print(
                f"WARNING: Metabase setup incomplete: {error_str}. "
                "Continuing without Metabase — retry on next restart.",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"WARNING: Metabase setup failed: {exc}. "
            "Continuing without Metabase — retry on next restart.",
            file=sys.stderr,
        )

    # 6. Dashboard import (non-critical)
    try:
        import_dashboards(project_root)
    except Exception:
        pass

    # H4: Check port availability before starting uvicorn
    _check_port(effective_port)

    # H3: Wrap uvicorn in try/finally for Docker cleanup
    import uvicorn

    print(f"Starting Dango on {host}:{effective_port}")
    try:
        uvicorn.run(
            "dango.web.app:app",
            host=host,
            port=effective_port,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    finally:
        _stop_docker_quiet(project_root)


def _check_port(port: int) -> None:
    """Exit with a clean error if *port* is already in use."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            print(f"Port {port} is already in use", file=sys.stderr)
            raise SystemExit(1) from None


# M5: typed as Path, not object
def _stop_docker_quiet(project_root: Path) -> None:
    """Best-effort Docker service cleanup."""
    try:
        from dango.platform import DockerManager

        DockerManager(project_root).stop_services()
    except Exception:
        pass
