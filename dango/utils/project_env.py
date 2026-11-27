"""dango/utils/project_env.py

The safe reader and writer for the project ``.env`` file.

Covered: every python-dotenv call in ``dango/`` (``load_dotenv``, ``dotenv_values``, ``get_key``,
``set_key``, ``unset_key``, ``DotEnv``, ``find_dotenv``; a repo-wide scanner test rejects them
outside this module) and the ``.env`` reads of the source wizard, source removal and the web
secrets API (:func:`read_env_text`). The file is read as UTF-8, and a file that cannot be read
(cp1252 text saved by Notepad, UTF-16, a NUL byte, a permission error) raises one
:class:`EnvFileError` whose text names the file and says to save it as plain UTF-8. Writes
(:func:`set_env_value`, :func:`unset_env_value`) check the file first, so a file that cannot be
read is never rewritten, and python-dotenv replaces the file only after a complete temporary copy
exists (a failed write leaves it byte-identical). Callers decide how to show the error: commands
print it and abort, MCP tools return it as the failed status, library code turns it into a failed
result, the web API returns a 422.

Level 0 (standard library and ``python-dotenv`` only). Runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

# Values python-dotenv reads back unchanged without quotes (no space, quote, backslash, ``$``, ``#``).
_BARE_VALUE = re.compile(r"[A-Za-z0-9_./:@+,-]+")


def _resolve_quote_mode(value: str, quote_mode: str) -> str:
    """``"auto"`` becomes ``"never"`` for a value that is safe bare, else ``"always"``."""
    if quote_mode == "auto":
        return "never" if _BARE_VALUE.fullmatch(value) else "always"
    return quote_mode


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

    def __init__(
        self,
        path: Path,
        cause: UnicodeDecodeError | ValueError | OSError,
        *,
        action: str = "read",
    ) -> None:
        reason, hint = describe_env_read_error(cause, None)
        text = f"Cannot {action} {path}: {reason}"
        super().__init__(f"{text} {hint}" if hint else text)
        self.path = path
        self.cause = cause
        self.__cause__ = cause


def _load(path: Path, override: bool) -> None:
    """``load_dotenv`` with every read failure mapped; the environment is restored on failure."""
    from dotenv import load_dotenv

    before = dict(os.environ)
    try:
        load_dotenv(path, override=override, encoding="utf-8")
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        # Nothing from a file that failed may stay in (or change) the environment.
        for name in set(os.environ) - set(before):
            del os.environ[name]
        for name, value in before.items():
            if os.environ.get(name) != value:
                os.environ[name] = value
        raise EnvFileError(path, exc) from exc


def _read(path: Path) -> dict[str, str | None]:
    """``dotenv_values`` with every read failure mapped. Never touches the environment."""
    from dotenv import dotenv_values

    try:
        values = dict(dotenv_values(path, encoding="utf-8"))
        _reject_nul(values)
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        raise EnvFileError(path, exc) from exc
    return values


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
    _load(project_root / ".env", override)


def read_env(project_root: Path) -> dict[str, str | None]:
    """The variables in ``<project>/.env`` without touching the environment.

    Returns:
        Name to value (None for a name without ``=``); empty when the file is missing.

    Raises:
        EnvFileError: The file cannot be read. The process environment is never touched.
    """
    return _read(project_root / ".env")


def env_has(project_root: Path, name: str) -> bool:
    """True when ``name`` has a value in the process environment or in ``<project>/.env``.

    Raises:
        EnvFileError: The file cannot be read (only checked when the environment lacks the name).
    """
    return bool(os.environ.get(name)) or bool(read_env(project_root).get(name))


def parse_env_text(text: str, *, interpolate: bool = True) -> dict[str, str | None]:
    """Parse ``.env`` text that the caller already decoded (same rules as :func:`read_env`).

    ``interpolate=False`` returns ``${NAME}`` references as written instead of expanding them
    from this machine's environment (for text that belongs to another machine).
    """
    from dotenv import dotenv_values

    return dict(dotenv_values(stream=io.StringIO(text), interpolate=interpolate))


def read_env_text(env_file: Path) -> str:
    """The text of an ``.env`` file, read as UTF-8, for callers that parse it themselves.

    Same newline handling as ``Path.read_text``. A missing file is the empty string.

    Raises:
        EnvFileError: The file cannot be read (not UTF-8, a NUL byte, a permission error).
    """
    try:
        with open(env_file, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return ""
    except (UnicodeDecodeError, ValueError, OSError) as exc:
        raise EnvFileError(env_file, exc) from exc
    if "\x00" in text:
        raise EnvFileError(env_file, ValueError("embedded null byte"))
    return text


def set_env_value(env_file: Path, key: str, value: str, *, quote_mode: str = "always") -> None:
    """Add or replace ``key`` in an ``.env`` file, exactly as ``dotenv.set_key`` does.

    ``quote_mode``: ``"always"`` (default) single-quotes every value; ``"auto"`` leaves a value of
    letters, digits and ``_ . / : @ + , -`` bare and single-quotes anything else. Every other line, comment and blank line is kept as it was.

    The file is created when it does not exist. An existing file is read first, so one that
    cannot be read is reported and never rewritten. python-dotenv then writes a temporary copy
    in the same folder and replaces the file only when that copy is complete: a failure leaves
    the existing file byte-identical.

    Raises:
        EnvFileError: The file cannot be read, or the replacement cannot be written.
    """
    from dotenv import set_key

    read_env_text(env_file)
    try:
        set_key(
            str(env_file),
            key,
            value,
            quote_mode=_resolve_quote_mode(value, quote_mode),
            encoding="utf-8",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvFileError(env_file, exc) from exc
    except OSError as exc:
        raise EnvFileError(env_file, exc, action="write") from exc


def unset_env_value(env_file: Path, key: str) -> bool:
    """Remove ``key`` from an ``.env`` file, exactly as ``dotenv.unset_key`` does.

    Returns:
        True when the key was removed; False when the file or the key does not exist.

    Raises:
        EnvFileError: The file cannot be read, or the replacement cannot be written.
    """
    from dotenv import unset_key

    if not env_file.exists():
        return False
    read_env_text(env_file)
    try:
        removed, _key = unset_key(str(env_file), key, encoding="utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvFileError(env_file, exc) from exc
    except OSError as exc:
        raise EnvFileError(env_file, exc, action="write") from exc
    return bool(removed)


def _quoted(value: str, quote_mode: str) -> str:
    """The right-hand side ``dotenv.set_key`` writes: single-quoted, backslash and quote escaped."""
    if _resolve_quote_mode(value, quote_mode) == "never":
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def set_env_text(text: str, key: str, value: str, *, quote_mode: str = "always") -> str:
    """``text`` with ``key`` added or replaced, as :func:`set_env_value` does to a file.

    For ``.env`` text that lives elsewhere (read and written over SSH): the new line is rendered
    with the quoting ``dotenv.set_key`` uses, every other line, comment and blank line is returned
    byte-for-byte, and nothing touches the disk.
    """
    from dotenv.parser import parse_stream

    if quote_mode not in ("always", "auto", "never"):
        raise ValueError(f"Unknown quote_mode: {quote_mode}")
    line = f"{key}={_quoted(value, quote_mode)}\n"
    out: list[str] = []
    replaced = False
    missing_newline = False
    for binding in parse_stream(io.StringIO(text)):
        if binding.key == key:
            out.append(line)
            replaced = True
        else:
            out.append(binding.original.string)
            missing_newline = not binding.original.string.endswith("\n")
    if not replaced:
        if missing_newline:
            out.append("\n")
        out.append(line)
    return "".join(out)


def unset_env_text(text: str, key: str) -> str:
    """``text`` without ``key`` (every other line byte-for-byte); unchanged when it is absent."""
    from dotenv.parser import parse_stream

    return "".join(
        binding.original.string for binding in parse_stream(io.StringIO(text)) if binding.key != key
    )
