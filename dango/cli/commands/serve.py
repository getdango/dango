"""dango/cli/commands/serve.py

Production foreground server command for cloud deployments.

``dango serve`` runs the Dango web platform in the foreground (under
systemd on the server).  Unlike ``dango start`` it does not daemonise,
open a browser, or start the file watcher.

Reuses all startup helpers from ``dango.platform.common.startup``.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import click


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Port (default: config value or 8800)")
@click.option("--workers", default=None, type=int, help="Number of uvicorn workers (default: 1)")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int | None, workers: int | None) -> None:
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
    from dango.platform.common.metabase_credential_migration import (
        complete_metabase_credential_migration,
    )
    from dango.platform.common.startup import (
        check_duckdb_version_alignment,
        cleanup_stale_dbt_lock,
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
    effective_workers = workers if workers is not None else (config.platform.workers or 1)
    if effective_workers < 1:
        effective_workers = 1

    # 0. Version alignment check — must run BEFORE any DuckDB write operations
    # because write mode auto-migrates the file format irreversibly.
    try:
        check_duckdb_version_alignment()
    except Exception as exc:
        from dango.exceptions import VersionMismatchError

        if isinstance(exc, VersionMismatchError):
            print(f"DuckDB version mismatch: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        raise

    # 0.5. Log rotation (never-fail)
    rotate_logs(project_root)

    # 1. Migrations
    try:
        run_pending_migrations(project_root)
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # Clean up stale dbt lock from crashed process
    if cleanup_stale_dbt_lock(project_root):
        print("WARNING: Removed stale dbt lock from crashed process", file=sys.stderr)

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

    # Docker assigns/repairs the persisted project ID during service start.
    # Apply the additive local-artifact protections for direct-pip users too.
    # A Git-ignore problem must never cause the production service to fail.
    try:
        from dango.config.credentials import ensure_sensitive_artifact_gitignores

        if ensure_sensitive_artifact_gitignores(project_root):
            print("Added sensitive local-artifact ignore rules to .gitignore.", file=sys.stderr)
    except Exception:
        print(
            "WARNING: Could not update .gitignore with sensitive local-artifact rules. "
            "Dango will retry safely on the next restart.",
            file=sys.stderr,
        )

    from dango.security.legacy_backup_artifacts import legacy_backup_artifact_warning

    if warning := legacy_backup_artifact_warning(project_root):
        print(f"WARNING: {warning}", file=sys.stderr)

    # The migration below and the dashboard import further down authenticate to
    # Metabase, which is still booting right after the Docker services start.
    try:
        from dango.platform.common.startup import wait_for_metabase_if_needed

        if wait_for_metabase_if_needed(project_root) is False:
            print(
                "WARNING: Metabase was not ready after 120s; credential migration and "
                "dashboard import will retry on the next restart.",
                file=sys.stderr,
            )
    except Exception:
        pass

    # Complete a pending legacy credential migration only after that identity
    # exists, and before setup decides whether Metabase needs configuration.
    credential_check_needed = False
    try:
        credential_migration = complete_metabase_credential_migration(project_root)
        credential_check_needed = credential_migration.get("status") == "not_required"
        if credential_migration.get("status") == "failed_non_destructive":
            from dango.platform.common.metabase_credential_migration import (
                describe_migration_failure,
            )

            message, retryable = describe_migration_failure(credential_migration)
            if retryable:
                # serve never repairs, so "will retry on the next start" would be false here.
                message = message.replace(" and will retry on the next start", "")
            print(f"WARNING: {message}\n{_repair_instructions()}", file=sys.stderr)
    except Exception:
        print(
            "WARNING: Metabase credential migration is incomplete; "
            "existing configuration is unchanged and will retry on the next restart.",
            file=sys.stderr,
        )

    # Detect (never repair) a missing, unreadable or rejected Metabase admin credential:
    # the state a restore or migrate leaves behind, which the migration above reports as
    # "not required". At most one login attempt per start (so only when the migration,
    # which logs in itself, did not run); never blocks startup.
    watch_credential = False
    if credential_check_needed:
        try:
            from dango.platform.common.startup import metabase_admin_credential_state

            credential_state = metabase_admin_credential_state(project_root, probe=False)
            credential_problem = {
                "missing": "No stored Metabase admin credential was found.",
                "unreadable": "The stored Metabase admin credential is unreadable.",
            }.get(credential_state)
            if credential_problem is not None:
                print(f"WARNING: {credential_problem} {_repair_instructions()}", file=sys.stderr)
            watch_credential = credential_state == "unverified"
        except Exception:
            pass

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
    except Exception:  # noqa: BLE001
        pass

    # H4: Check port availability before starting uvicorn
    _check_port(effective_port)

    if watch_credential:
        # The login check needs a healthy Metabase, which is usually still booting here.
        # Run it off the startup path: no delay before the port binds.
        threading.Thread(
            target=_watch_for_rejected_credential, args=(project_root,), daemon=True
        ).start()

    # Set project root env var so uvicorn workers can resolve it on import
    import os

    os.environ["DANGO_PROJECT_ROOT"] = str(project_root)

    # H3: Wrap uvicorn in try/finally for Docker cleanup
    import uvicorn

    worker_msg = f" ({effective_workers} workers)" if effective_workers > 1 else ""
    print(f"Starting Dango on {host}:{effective_port}{worker_msg}")
    try:
        uvicorn_kwargs: dict[str, Any] = {
            "host": host,
            "port": effective_port,
            "log_level": "info",
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1",
        }
        if effective_workers > 1:
            uvicorn_kwargs["workers"] = effective_workers
        uvicorn.run("dango.web.app:app", **uvicorn_kwargs)
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
    except Exception:  # noqa: BLE001
        pass


def _repair_instructions() -> str:
    """Return the operator text naming both ways to repair Metabase admin access."""
    from dango.platform.common.startup import cloud_repair_admin_command

    return (
        "Metabase admin access needs repair. On this server run:\n"
        f"  {cloud_repair_admin_command()}\n"
        "or from your computer:\n"
        "  dango remote metabase-repair-admin\n"
        "(Running it as root would leave root-owned files in the credential store.)"
    )


def _watch_for_rejected_credential(project_root: Path) -> None:
    """Wait (bounded) for Metabase health, then check the admin credential once.

    Daemon-thread body: polls only ``/api/health`` (no login) every 5 s for up to 180 s,
    then makes the single login of ``metabase_admin_credential_state``. Prints the repair
    commands only for ``rejected``; prints nothing if Metabase never becomes healthy. Never
    raises and never prints a password.
    """
    try:
        import requests

        from dango.platform.common.startup import metabase_admin_credential_state
        from dango.security.metabase_config import load_metabase_metadata

        metadata = load_metabase_metadata(project_root) or {}
        url = str(metadata.get("metabase_url") or "http://localhost:3000").rstrip("/")
        for _attempt in range(36):
            try:
                if requests.get(f"{url}/api/health", timeout=5).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(5)
        else:
            return
        if metabase_admin_credential_state(project_root) == "rejected":
            print(
                f"WARNING: Metabase rejected the stored admin credential. {_repair_instructions()}",
                file=sys.stderr,
            )
    except Exception:
        pass
