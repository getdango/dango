"""dango/utils/env_file.py

Shared helpers for reading and serializing ``.env`` content.

Used by ``web/routes/secrets.py`` (local file I/O), ``web/routes/oauth_connect.py`` (credential
lookup), ``cli/commands/remote_env.py`` (remote file I/O via SSH) and the backup, redaction and
source-removal readers. To change an existing ``.env`` use ``project_env.set_env_value`` /
``unset_env_value`` (files) or ``set_env_text`` / ``unset_env_text`` (text): they keep comments,
order and every other line. ``serialize_env_file`` rebuilds a file from a dict and drops them.
"""

from __future__ import annotations

from dango.utils.project_env import parse_env_text


def parse_env_file(content: str) -> dict[str, str]:
    """Parse ``.env`` content into a dict the way the runtime (python-dotenv) reads it.

    Handles quoting, escapes and multi-line values; ignores blank lines, ``#`` comments and names
    without ``=``. ``${NAME}`` references are returned as written (the text may belong to another
    machine).
    """
    return {
        key: value
        for key, value in parse_env_text(content, interpolate=False).items()
        if value is not None
    }


def parse_env_simple(content: str) -> dict[str, str]:
    """Parse ``.env`` content with the standard library only (no escapes, one line per value).

    The source of this function is embedded in scripts that run on servers with an older Dango
    version (``platform/cloud/migrate.py``), so it must stay free of imports and of references to
    other Dango code. Everything that runs locally uses :func:`parse_env_file`.
    """
    env_vars: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            env_vars[key] = value
    return env_vars


def serialize_env_file(env_vars: dict[str, str]) -> str:
    """Serialize a dict back to ``.env`` format.

    Values containing spaces, quotes, or special characters are double-quoted with ``\\`` and
    ``"`` escaped (what :func:`parse_env_file` reads back). Comments and ordering of an existing
    file are not preserved: do not use this to rewrite a user's ``.env``.
    """
    lines: list[str] = []
    for key, value in env_vars.items():
        needs_quoting = any(c in value for c in (" ", '"', "'", "#", "$", "\n", "\t"))
        if needs_quoting:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n" if lines else ""
