"""dango/utils/project_env.py

The one safe reader for the project ``.env`` file.

Every read of ``<project>/.env`` goes through here (a repo-wide scanner test rejects bare
python-dotenv calls anywhere else). The file is read as UTF-8, and a file that cannot be read
(cp1252 text saved by Notepad, UTF-16, a NUL byte, a permission error) raises one
:class:`EnvFileError` whose text names the file and says to save it as plain UTF-8. Callers
decide how to show it: commands print it and abort, MCP tools return it as the failed status,
library code turns it into a failed result.

Level 0 (standard library and ``python-dotenv`` only). Runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import io
import os
from pathlib import Path


def describe_env_read_error(
    exc: UnicodeDecodeError | ValueError | OSError, command: str | None
) -> tuple[str, str | None]:
    """Plain-text reason a project ``.env`` could not be read, and what to do about it.

    Args:
        exc: What the read raised.
        command: The command to name in the hint (``start``, ``sync``, ...); None for a generic
            "try again" hint.

    Returns:
        ``(reason, hint)``; the hint is None when the operating system's message says it all.
    """
    again = f"run 'dango {command}' again" if command else "try again"
    if isinstance(exc, UnicodeDecodeError):
        return ("it must be UTF-8 text.", f"Save the file as UTF-8, then {again}.")
    if isinstance(exc, ValueError):
        # A NUL byte (also what UTF-16 text without a BOM decodes to) or an oversized value.
        return (
            "it is not a readable UTF-8 text file.",
            f"Save the file as plain UTF-8 text, then {again}.",
        )
    return (exc.strerror or "read error", None)


class EnvFileError(Exception):
    """The project ``.env`` cannot be read.

    ``str()`` is plain text (``Cannot read <path>: <reason>`` and the hint); escape it before
    printing it through Rich.
    """

    def __init__(self, path: Path, cause: UnicodeDecodeError | ValueError | OSError) -> None:
        reason, hint = describe_env_read_error(cause, None)
        text = f"Cannot read {path}: {reason}"
        super().__init__(f"{text} {hint}" if hint else text)
        self.path = path
        self.cause = cause
        self.__cause__ = cause


def _snapshot_and_run(path: Path, action: str, override: bool) -> dict[str, str | None]:
    """Run the python-dotenv call; map every read failure to :class:`EnvFileError`."""
    from dotenv import dotenv_values, load_dotenv

    before = dict(os.environ)
    try:
        if action == "load":
            load_dotenv(path, override=override, encoding="utf-8")
            return {}
        values = dict(dotenv_values(path, encoding="utf-8"))
        _reject_nul(values)
        return values
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        # Nothing from a file that failed may stay in (or change) the environment.
        for name in set(os.environ) - set(before):
            del os.environ[name]
        for name, value in before.items():
            if os.environ.get(name) != value:
                os.environ[name] = value
        raise EnvFileError(path, exc) from exc


def _reject_nul(values: dict[str, str | None]) -> None:
    """Raise ValueError for a name or value the operating system would reject."""
    for name, value in values.items():
        if "\x00" in name or (value is not None and "\x00" in value):
            raise ValueError("embedded null byte")


def load_env(project_root: Path, *, override: bool = False) -> None:
    """Load ``<project>/.env`` into the process environment.

    Parsing, interpolation, BOM and CRLF handling are python-dotenv's. A missing file (or a
    directory named ``.env``) loads nothing.

    Args:
        project_root: Project root directory.
        override: Replace variables that are already set (default: existing variables win).

    Raises:
        EnvFileError: The file cannot be read; the environment is left as it was.
    """
    _snapshot_and_run(project_root / ".env", "load", override)


def read_env(project_root: Path) -> dict[str, str | None]:
    """The variables in ``<project>/.env`` without touching the environment.

    Returns:
        Name to value (None for a name without ``=``); empty when the file is missing.

    Raises:
        EnvFileError: The file cannot be read.
    """
    return _snapshot_and_run(project_root / ".env", "read", False)


def env_has(project_root: Path, name: str) -> bool:
    """True when ``name`` has a value in the process environment or in ``<project>/.env``.

    Raises:
        EnvFileError: The file cannot be read (only checked when the environment lacks the name).
    """
    return bool(os.environ.get(name)) or bool(read_env(project_root).get(name))


def parse_env_text(text: str) -> dict[str, str | None]:
    """Parse ``.env`` text that the caller already decoded (same rules as :func:`read_env`)."""
    from dotenv import dotenv_values

    return dict(dotenv_values(stream=io.StringIO(text)))
