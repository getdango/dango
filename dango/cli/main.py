"""dango/cli/main.py

CLI entry point for the Dango data platform.

Registers all command modules from dango.cli.commands and provides the
top-level ``cli`` group with version info and project-root discovery.
"""

import click

from dango import __version__
from dango.cli.commands.analyze import analyze
from dango.cli.commands.auth import auth
from dango.cli.commands.cleanup import cleanup
from dango.cli.commands.config_cmd import config
from dango.cli.commands.dashboard import dashboard
from dango.cli.commands.data import db, validate
from dango.cli.commands.deploy import deploy
from dango.cli.commands.governance import governance
from dango.cli.commands.metabase_cmd import metabase
from dango.cli.commands.migrate import migrate
from dango.cli.commands.model import model
from dango.cli.commands.notebook import notebook, snapshot
from dango.cli.commands.oauth import oauth
from dango.cli.commands.platform import start, status, stop
from dango.cli.commands.project import info, init, rename
from dango.cli.commands.remote import remote
from dango.cli.commands.schedule import schedule
from dango.cli.commands.serve import serve
from dango.cli.commands.source import source, sync
from dango.cli.commands.transform import docs, generate, run
from dango.cli.commands.upgrade import upgrade
from dango.cli.commands.web import web


@click.group()
@click.version_option(version=__version__, prog_name="dango")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """
    🍡 Dango - Open source data platform

    Professional analytics stack built with production-grade tools.

    Common commands:
      dango init       Create a new data project
      dango start      Start data platform services
      dango sync       Load data from all sources
      dango status     Show platform status

    For help on a specific command:
      dango <command> --help
    """
    ctx.ensure_object(dict)

    # Warn if the dango binary doesn't match the active venv
    import os
    import sys

    venv_prefix = os.environ.get("VIRTUAL_ENV")
    if venv_prefix and not sys.executable.startswith(venv_prefix):
        click.echo(
            "Warning: Running 'dango' from outside the active venv. "
            "Run 'hash -r' or restart your terminal.",
            err=True,
        )

    # Try to find project root for commands that need it
    # (init command doesn't need it, but most others do)
    try:
        from dango.config.helpers import find_project_root

        ctx.obj["project_root"] = find_project_root()
    except Exception:
        # Not in a project - that's OK for some commands like init
        ctx.obj["project_root"] = None


# --- Register top-level commands ---
cli.add_command(analyze)
cli.add_command(init)
cli.add_command(rename)
cli.add_command(info)
cli.add_command(start)
cli.add_command(stop)
cli.add_command(status)
cli.add_command(sync)
cli.add_command(run)
cli.add_command(docs)
cli.add_command(generate)
cli.add_command(validate)
cli.add_command(web)
cli.add_command(serve)
cli.add_command(upgrade)
cli.add_command(cleanup)
cli.add_command(snapshot)

# --- Register command groups ---
cli.add_command(source)
cli.add_command(config)
cli.add_command(db)
cli.add_command(auth)
cli.add_command(oauth)
cli.add_command(model)
cli.add_command(dashboard)
cli.add_command(metabase)
cli.add_command(migrate)
cli.add_command(remote)
cli.add_command(notebook)
cli.add_command(schedule)
cli.add_command(deploy)
cli.add_command(governance)


def main() -> None:
    """Entry point for CLI."""
    cli(obj={})


if __name__ == "__main__":
    main()
