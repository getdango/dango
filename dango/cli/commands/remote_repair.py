"""dango/cli/commands/remote_repair.py

Remote server repair and recovery commands.

These commands are registered on the ``remote`` group defined in
``remote.py`` via ``@remote.command()`` decorators.  The parent module
triggers registration by importing this module at the bottom of ``remote.py``.
"""

from __future__ import annotations

import hashlib

import click

from dango.cli import console
from dango.cli.commands.remote import remote


@remote.command("repair")
@click.pass_context
def remote_repair(ctx: click.Context) -> None:
    """Diagnose and repair common cloud deployment issues.

    Checks disk, RAM, DNS, restarts services, re-runs Metabase setup
    if metabase.yml is missing, and triggers a Metabase schema scan.
    """
    from dango.cli.commands.remote_mgmt import (
        _load_cloud_config_with_ip,
        _make_ssh_manager,
    )

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)
    ssh = _make_ssh_manager(cloud_cfg, project_root)

    try:
        ssh.connect(cloud_cfg.droplet_ip)
        console.print(f"Connected to [bold]{cloud_cfg.droplet_ip}[/bold]\n")

        issues_found = 0

        # 1. Check disk space
        console.print("[bold]Checking disk space...[/bold]")
        disk_result = ssh.exec_command(
            "df / --output=avail -BM 2>/dev/null | tail -1 | tr -d ' M'",
            timeout=10,
        )
        if disk_result.success and disk_result.stdout.strip().isdigit():
            disk_mb = int(disk_result.stdout.strip())
            if disk_mb < 5000:
                console.print(
                    f"  [red]Low disk:[/red] {disk_mb} MB available. "
                    "Consider cleaning up with: docker system prune -f"
                )
                issues_found += 1
            else:
                console.print(f"  [green]OK[/green] — {disk_mb} MB available")

        # 2. Check RAM
        console.print("[bold]Checking RAM...[/bold]")
        ram_result = ssh.exec_command("free -m | awk '/Mem:/ {print $2, $7}'", timeout=10)
        if ram_result.success and ram_result.stdout.strip():
            parts = ram_result.stdout.strip().split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                total_mb, avail_mb = int(parts[0]), int(parts[1])
                if avail_mb < 500:
                    console.print(
                        f"  [red]Low memory:[/red] {avail_mb} MB available of {total_mb} MB"
                    )
                    issues_found += 1
                else:
                    console.print(f"  [green]OK[/green] — {avail_mb} MB available of {total_mb} MB")

        # 3. Check DNS
        console.print("[bold]Checking DNS...[/bold]")
        dns_result = ssh.exec_command(
            "cat /etc/resolv.conf 2>/dev/null | grep -c nameserver",
            timeout=10,
        )
        if dns_result.success and dns_result.stdout.strip() == "0":
            console.print("  [red]DNS broken:[/red] /etc/resolv.conf has no nameservers. Fixing...")
            ssh.exec_command("echo 'nameserver 8.8.8.8' > /etc/resolv.conf", timeout=10)
            console.print("  [green]Fixed[/green] — added Google DNS")
            issues_found += 1
        elif not dns_result.success:
            console.print("  [red]DNS broken:[/red] /etc/resolv.conf missing. Fixing...")
            ssh.exec_command("echo 'nameserver 8.8.8.8' > /etc/resolv.conf", timeout=10)
            console.print("  [green]Fixed[/green] — created /etc/resolv.conf")
            issues_found += 1
        else:
            console.print("  [green]OK[/green]")

        # 4. Restart services
        console.print("[bold]Restarting services...[/bold]")

        _server_project_dir = "/srv/dango/project"
        _proj_hash = hashlib.md5(_server_project_dir.encode(), usedforsecurity=False).hexdigest()[
            :8
        ]
        _proj_name = f"dango-{_proj_hash}"

        # Start Docker containers
        ssh.exec_command(
            f"cd {_server_project_dir} && "
            f"COMPOSE_PROJECT_NAME={_proj_name} docker compose start metabase "
            "2>/dev/null || true",
            timeout=120,
        )

        # Restart dango-web
        result = ssh.exec_command("systemctl restart dango-web", timeout=30)
        if result.success:
            console.print("  [green]OK[/green] — dango-web restarted")
        else:
            console.print(f"  [red]Failed:[/red] dango-web restart — {result.stderr.strip()}")
            issues_found += 1

        # 5. Check Metabase setup
        console.print("[bold]Checking Metabase...[/bold]")
        mb_check = ssh.exec_command(
            f"test -f {_server_project_dir}/.dango/metabase.yml",
            timeout=10,
        )
        if not mb_check.success:
            console.print(
                "  [yellow]metabase.yml missing[/yellow] — Metabase setup incomplete. "
                "Run [bold]dango remote reset-metabase[/bold] to fix."
            )
            issues_found += 1
        else:
            console.print("  [green]OK[/green] — metabase.yml present")

            # Trigger schema scan
            console.print("[bold]Triggering Metabase schema scan...[/bold]")
            scan_result = ssh.exec_command(
                f"cd {_server_project_dir} && "
                f'/srv/dango/venv/bin/python -c "'
                "import yaml, requests, time; "
                "creds = yaml.safe_load(open('.dango/metabase.yml')); "
                "email = creds.get('admin', {}).get('email'); "
                "pw = creds.get('admin', {}).get('password'); "
                "db_id = creds.get('database', {}).get('id'); "
                "[exit(0) for _ in range(1) if not all([email, pw, db_id])]; "
                "r = requests.post('http://localhost:3000/api/session', "
                "json={'username': email, 'password': pw}, timeout=10); "
                "sid = r.json().get('id') if r.status_code == 200 else None; "
                "[exit(0) for _ in range(1) if not sid]; "
                "requests.post(f'http://localhost:3000/api/database/{db_id}/sync_schema', "
                "headers={'X-Metabase-Session': sid}, timeout=10)"
                '"',
                timeout=30,
            )
            if scan_result.success:
                console.print("  [green]OK[/green] — schema scan triggered")
            else:
                console.print(
                    "  [yellow]Warning:[/yellow] Could not trigger schema scan "
                    "(Metabase may still be starting)"
                )

        # Summary
        console.print()
        if issues_found == 0:
            console.print("[green]No issues found.[/green] Server is healthy.")
        else:
            console.print(
                f"[yellow]{issues_found} issue(s) found and addressed.[/yellow] "
                "Run [bold]dango remote status[/bold] to verify."
            )

    except Exception as exc:
        console.print(f"[red]Error:[/red] Could not connect to server: {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()


@remote.command("reset-metabase")
@click.pass_context
def remote_reset_metabase(ctx: click.Context) -> None:
    """Reset Metabase to a fresh state without losing warehouse data.

    Stops Metabase, removes its Docker volume (H2 database), restarts
    it, and lets dango-web re-run Metabase setup on next startup.
    """
    from dango.cli.commands.remote_mgmt import (
        _load_cloud_config_with_ip,
        _make_ssh_manager,
    )

    cloud_cfg, project_root = _load_cloud_config_with_ip(ctx)

    console.print(
        "[yellow]This will reset Metabase to factory state.[/yellow]\n"
        "  - All Metabase dashboards and questions will be lost\n"
        "  - Your DuckDB warehouse data is NOT affected\n"
        "  - Admin account will be re-created on next startup\n"
    )

    confirm = click.prompt("Type 'reset' to confirm", default="", show_default=False)
    if confirm != "reset":
        console.print("[yellow]Aborted.[/yellow]")
        return

    ssh = _make_ssh_manager(cloud_cfg, project_root)

    try:
        ssh.connect(cloud_cfg.droplet_ip)

        _server_project_dir = "/srv/dango/project"
        _proj_hash = hashlib.md5(_server_project_dir.encode(), usedforsecurity=False).hexdigest()[
            :8
        ]
        _proj_name = f"dango-{_proj_hash}"

        # 1. Stop dango-web (so it doesn't interfere with Metabase restart)
        console.print("Stopping dango-web...")
        ssh.exec_command("systemctl stop dango-web 2>/dev/null || true", timeout=15)

        # 2. Stop and remove Metabase container + volume
        console.print("Removing Metabase data...")
        ssh.exec_command(
            f"cd {_server_project_dir} && "
            f"COMPOSE_PROJECT_NAME={_proj_name} docker compose down metabase --volumes "
            "2>/dev/null || true",
            timeout=60,
        )

        # 3. Remove metabase.yml so setup re-runs
        console.print("Removing metabase.yml...")
        ssh.exec_command(
            f"rm -f {_server_project_dir}/.dango/metabase.yml",
            timeout=10,
        )

        # 4. Restart dango-web (triggers Docker rebuild + Metabase setup)
        console.print("Restarting dango-web (this may take a few minutes)...")
        ssh.exec_command("systemctl start dango-web", timeout=30)

        # 5. Wait for Metabase to become ready, then re-sync all users
        import time

        console.print("Waiting for Metabase to initialize...")
        _server_project_dir = "/srv/dango/project"
        for _attempt in range(24):  # up to 2 minutes
            time.sleep(5)
            health = ssh.exec_command(
                "curl -sf http://localhost:3000/api/health -o /dev/null && echo ok",
                timeout=5,
            )
            if health.success and "ok" in health.stdout:
                break
        else:
            console.print(
                "[yellow]Warning:[/yellow] Metabase did not become ready in time. "
                "SSO may need manual re-sync."
            )

        # Re-sync all Dango users to fresh Metabase instance
        console.print("Re-syncing users to Metabase...")
        sync_result = ssh.exec_command(
            f"cd {_server_project_dir} && "
            '/srv/dango/venv/bin/python -c "'
            "from pathlib import Path; "
            "from dango.auth.admin import get_auth_db_path; "
            "from dango.auth.metabase_sync import sync_all_users_to_metabase; "
            "p = Path('.'); "
            "r = sync_all_users_to_metabase(get_auth_db_path(p), p, 'http://localhost:3000'); "
            'print(f\'Synced: {r[\\"synced\\"]}, Created: {r[\\"created\\"]}\')'
            '"',
            timeout=60,
        )
        if sync_result.success:
            console.print(f"  [green]OK[/green] — {sync_result.stdout.strip()}")
        else:
            console.print(
                "[yellow]Warning:[/yellow] User re-sync failed. "
                "Users may need to be re-added to Metabase manually."
            )

        console.print(
            "\n[green]Metabase reset complete.[/green]\n"
            "Log out and log back in to restore Metabase SSO.\n"
            "Run [bold]dango remote status[/bold] to check progress."
        )

    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        ssh.disconnect()
