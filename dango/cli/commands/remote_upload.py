"""dango/cli/commands/remote_upload.py

Remote upload command: put one local file under the server's ``data/`` directory.

Registered on the ``remote`` group defined in ``remote.py`` via a
``@remote.command()`` decorator.  The parent module triggers registration
by importing this module at the bottom of ``remote.py``.

Everything under ``data/`` is gitignored and never pushed by ``remote push``;
this command (or the web UI CSV upload) is the way to place a file there.
"""

from __future__ import annotations

import contextlib
import os
import posixpath
import shlex
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from dango.cli import console
from dango.cli.commands.remote import remote

REMOTE_PROJECT_DIR = "/srv/dango/project"
MAX_UPLOAD_BYTES = 2 * 1024**3

_ONLY_DATA = "only files under data/ can be uploaded; use `dango remote push` for code"


def resolve_remote_target(local_path: Path, remote_path: str | None, project_root: Path) -> str:
    """Return an absolute server path under ``/srv/dango/project/data``.

    Args:
        local_path: The local file being uploaded.
        remote_path: Destination relative to the project root (must be under
            ``data/``).  Defaults to *local_path* relative to *project_root*.
        project_root: Local project root.

    Raises:
        click.UsageError: If the destination is not a safe path under ``data/``.
    """
    if remote_path is None:
        try:
            rel = Path(os.path.abspath(local_path)).relative_to(os.path.abspath(project_root))
        except ValueError:
            raise click.UsageError(_ONLY_DATA) from None
        remote_path = rel.as_posix()

    if not remote_path or "\x00" in remote_path:
        raise click.UsageError("Invalid remote path.")
    if remote_path.startswith("/"):
        raise click.UsageError(
            "The remote path must be relative to the project root, not absolute."
        )
    if ".." in remote_path.split("/"):
        raise click.UsageError("The remote path must not contain '..'.")

    normalized = posixpath.normpath(remote_path)
    if not normalized.startswith("data/"):
        raise click.UsageError(_ONLY_DATA)
    rest = normalized[len("data/") :]
    first = rest.split("/", 1)[0]
    if first.startswith("warehouse.duckdb"):
        raise click.UsageError("The DuckDB warehouse files cannot be uploaded.")
    if first == ".tmp":
        raise click.UsageError("data/.tmp is reserved for the server and cannot be uploaded to.")
    return f"{REMOTE_PROJECT_DIR}/{normalized}"


def _validate_local_file(local_path: Path) -> int:
    """Return the size of *local_path* after checking it is a regular, non-link file."""
    if local_path.is_symlink():
        raise click.UsageError(f"{local_path} is a symbolic link; upload the real file.")
    if local_path.is_dir():
        raise click.UsageError(f"{local_path} is a directory; only single files can be uploaded.")
    if not local_path.is_file():
        raise click.UsageError(f"{local_path} does not exist or is not a regular file.")
    size = local_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise click.UsageError(
            f"{local_path.name} is {size / 1024**3:.1f} GiB; the upload limit is 2 GiB."
        )
    return size


def _cleanup_part(ssh: Any, part: str) -> None:
    """Best-effort removal of the partial upload; never masks the original error."""
    with contextlib.suppress(Exception):
        ssh.exec_command(f"rm -f {shlex.quote(part)}")


@remote.command("upload")
@click.argument("local_path", type=click.Path(path_type=Path))
@click.argument("remote_path", required=False)
@click.pass_context
def remote_upload(ctx: click.Context, local_path: Path, remote_path: str | None) -> None:
    """Upload one local file into the server's data/ directory.

    REMOTE_PATH is relative to the project root and must be under data/;
    it defaults to the file's own path inside the project.

    Examples:

      dango remote upload data/uploads/orders.csv

      dango remote upload ~/Downloads/orders.csv data/uploads/orders.csv
    """
    from dango.cli.commands.remote import _ssh_connect_or_fail
    from dango.exceptions import CloudSSHError

    _cloud_cfg, ssh, project_root = _ssh_connect_or_fail(ctx)
    try:
        target = resolve_remote_target(local_path, remote_path, project_root)
        _validate_local_file(local_path)
        part = f"{target}.part"
        q = shlex.quote
        try:
            mkdir = ssh.exec_command(f"mkdir -p {q(posixpath.dirname(target))}")
            if mkdir.exit_code != 0:
                raise click.ClickException(f"Could not create the remote directory: {mkdir.stderr}")
            ssh.upload_file(local_path, part)
            moved = ssh.exec_command(f"mv -fT {q(part)} {q(target)}")
            if moved.exit_code != 0:
                ssh.exec_command(f"rm -f {q(part)}")
                raise click.ClickException(f"Could not move the upload into place: {moved.stderr}")
        except CloudSSHError as exc:
            ssh.exec_command(f"rm -f {q(part)}")
            raise click.ClickException(f"Upload failed: {exc}") from exc
        except BaseException:
            _cleanup_part(ssh, part)
            raise
        console.print(f"[green]Uploaded[/green] {escape(local_path.name)} -> {escape(target)}")
    finally:
        ssh.disconnect()
