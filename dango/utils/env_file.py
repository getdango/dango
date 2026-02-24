"""dango/utils/env_file.py

Shared helpers for parsing and serializing ``.env`` files.

Used by ``web/routes/secrets.py`` (local file I/O),
``web/routes/oauth_connect.py`` (credential lookup),
and ``cli/commands/remote_env.py`` (remote file I/O via SSH).
"""

from __future__ import annotations


def parse_env_file(content: str) -> dict[str, str]:
    """Parse ``.env`` content into a dict.

    Handles ``KEY=VALUE``, ``KEY="VALUE"``, ``KEY='VALUE'``.
    Skips blank lines and ``#`` comments.
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

    Values containing spaces, quotes, or special characters are
    double-quoted.  Simple values are written bare.
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
