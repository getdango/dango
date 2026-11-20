"""dango/utils/duckdb_lock_text.py

Recognise DuckDB's single-writer lock error and say what it means in plain words.

Level 0 module (no Dango imports at load time). Output is plain ASCII with no Rich markup: the only
text taken from the error is a numeric process id. The raw text goes to the debug log, redacted.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_DEPTH = 8

# "Could not set lock on file "<path>"" and what follows it on the same line (holder, PID, link).
_UNIX_START = re.compile(r'(?:IO Error: )?Could not set lock on file "[^"\n]*"')
_CONFLICT = ": Conflicting lock is held in "
_SEE_ALSO = re.compile(r"See also \S+")
_PID = re.compile(r"\(PID (\d+)\)")
# Windows wording when DuckDB can ask the OS which processes hold the file (Restart Manager):
# "File is already open in" and then one "<process> (PID n)" line per holder (possibly none).
# Written from DuckDB's source, not yet confirmed on Windows: scripts/check_duckdb_lock_text.py
# prints the real message on every platform-smoke leg.
_ALREADY_OPEN = re.compile(
    r"(?:IO Error: )?File is already open in[ \t]*(?:\r?\n[^\n]*?\(PID \d+\))*"
)
# The Windows fallback wording when that lookup is unavailable.
_WINDOWS = re.compile(
    r'(?:IO Error: )?Cannot open file "[^"\n]*\.duckdb(?:\.wal)?": The process cannot access '
    r"the file because it is being used by another process\.?"
)


def lock_message(pid: int | None = None) -> str:
    """Plain ASCII explanation of a warehouse lock; *pid* is shown only when given."""
    holder = f" (PID {pid})" if pid is not None else ""
    return (
        f"Another Dango process is using the warehouse{holder}: a sync, dbt run, notebook, "
        "web server or MCP session. Wait for it to finish, then run the command again. "
        "Run `dango status` to see what is running."
    )


def _segments(text: str) -> list[tuple[int, int, int | None]]:
    """(start, end, pid) of every lock-error passage in *text*."""
    found: list[tuple[int, int, int | None]] = []
    pos = 0
    for match in _UNIX_START.finditer(text):
        if match.start() < pos:
            continue
        end = match.end()
        pid: int | None = None
        if text.startswith(_CONFLICT, end):
            line_end = text.find("\n", end)
            line_end = len(text) if line_end < 0 else line_end
            line = text[end:line_end]
            see = _SEE_ALSO.search(line)
            end = end + see.end() if see else line_end
            pids = _PID.findall(text[match.end() : end])
            pid = int(pids[-1]) if pids else None
        else:
            see = _SEE_ALSO.match(text, end + 1) if text.startswith(" ", end) else None
            if see:
                end = see.end()
        found.append((match.start(), end, pid))
        pos = end
    for match in _ALREADY_OPEN.finditer(text):
        if not any(s <= match.start() < e for s, e, _ in found):
            pids = _PID.findall(match.group(0))
            found.append((match.start(), match.end(), int(pids[-1]) if pids else None))
    for match in _WINDOWS.finditer(text):
        if not any(s <= match.start() < e for s, e, _ in found):
            found.append((match.start(), match.end(), None))
    return sorted(found)


def is_lock_error_text(text: str) -> bool:
    """True when *text* contains DuckDB's lock-conflict wording."""
    return bool(_segments(text))


def lock_message_from_text(text: str) -> str | None:
    """The plain message for the first lock passage in *text* (e.g. a log excerpt), else None."""
    segments = _segments(text)
    if not segments:
        return None
    start, end, pid = segments[0]
    logger.debug("duckdb_lock_error_raw: %s", text[start:end])
    return lock_message(pid)


def rewrite_lock_text(text: str) -> str:
    """*text* with every DuckDB lock-conflict passage replaced by the plain explanation.

    Text around a passage is kept. The raw passage is written to the debug log.
    """
    segments = _segments(text)
    if not segments:
        return text
    out: list[str] = []
    pos = 0
    for start, end, pid in segments:
        logger.debug("duckdb_lock_error_raw: %s", text[start:end])
        out.append(text[pos:start])
        out.append(lock_message(pid))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _children(exc: BaseException, follow_context: bool = True) -> list[BaseException]:
    found: list[BaseException] = []
    context = exc.__context__ if follow_context else None
    for candidate in (exc.__cause__, context, *exc.args):
        if isinstance(candidate, BaseException):
            found.append(candidate)
    return found


def is_duckdb_lock_error(error: object, *, follow_context: bool = True) -> bool:
    """True when *error*, or an exception it wraps, is DuckDB's lock-conflict error.

    With ``follow_context=False`` an exception raised while another was being handled
    (implicit ``__context__``) is not looked through, only ``raise ... from`` causes and args.
    """
    if not isinstance(error, BaseException):
        return is_lock_error_text(str(error))
    seen: set[int] = set()
    stack: list[tuple[BaseException, int]] = [(error, 0)]
    while stack:
        current, depth = stack.pop()
        if id(current) in seen or depth > _MAX_DEPTH:
            continue
        seen.add(id(current))
        if is_lock_error_text(str(current)):
            return True
        stack.extend((child, depth + 1) for child in _children(current, follow_context))
    return False


def _lock_pid(error: object) -> int | None:
    """The holder's numeric pid from the first lock passage found in *error* or what it wraps."""
    texts = [str(error)]
    if isinstance(error, BaseException):
        texts.extend(str(child) for child in _children(error))
    for text in texts:
        for _start, _end, pid in _segments(text):
            return pid
    return None


def describe_error(error: object, project_root: Path | None = None) -> str:
    """Plain-language text for a lock error; ``str(error)`` for every other error.

    The raw text is written to the debug log with secrets redacted.
    """
    if not is_duckdb_lock_error(error):
        return str(error)
    from dango.utils.redact import redact_project_text

    logger.debug("duckdb_lock_error_raw: %s", redact_project_text(project_root, str(error)))
    return lock_message(_lock_pid(error))
