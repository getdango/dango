"""dango/platform/cloud/remote_launch.py

Shell-script builder for starting a long-running command in the background on a remote host.

Shared by ``dango remote sync`` and the MCP ``remote_sync`` tool. The builder is pure (no SSH):
callers send the result as ``sh -c <shlex.quote(script)>``.
"""

from __future__ import annotations

import shlex

DEFAULT_GRACE_SECONDS = 2


def build_background_launch(
    project_dir: str, command: str, grace_seconds: int = DEFAULT_GRACE_SECONDS
) -> str:
    """Return a shell script that starts ``command`` in the background inside ``project_dir``.

    ``command`` must already be fully quoted by the caller. Only ``command`` is backgrounded;
    the directory check and liveness verification run in the foreground so a failure is
    reported and the SSH channel is released as soon as the launcher exits.

    Exit status of the script: 0 = started (still running, or finished OK within the grace
    period); 2 = ``project_dir`` could not be entered; 3 = the command exited non-zero within
    the grace period (status on stderr).
    """
    quoted_dir = shlex.quote(project_dir)
    grace = int(grace_seconds)
    return (
        f'cd {quoted_dir} || {{ echo "cannot cd to "{quoted_dir} >&2; exit 2; }}\n'
        f"nohup {command} > /dev/null 2>&1 &\n"
        "pid=$!\n"
        # Silence the shell's own job-status line ("Killed: 9 <full command>"), which would
        # leak the command text; keep fd 3 as the real stderr for our own message.
        "exec 3>&2 2>/dev/null\n"
        f"sleep {grace}\n"
        'if kill -0 "$pid" 2>/dev/null; then echo "started pid=$pid"; exit 0; fi\n'
        'wait "$pid"; rc=$?\n'
        f'if [ "$rc" -eq 0 ]; then echo "started (finished within {grace}s)"; exit 0; fi\n'
        'echo "command exited early with status $rc" >&3; exit 3\n'
    )
