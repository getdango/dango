"""dango/utils/redact.py

Shared secret redaction for log text served by the web API and the MCP tools.

Level 0 module (standard library, ``toml`` and ``dango.utils.env_file`` only; ``tomllib`` is 3.11+). Exact known secret values are replaced with
``[REDACTED]``; secret-looking patterns (key=value pairs, URL credentials, auth headers,
signed query parameters, chat webhook URLs) are masked even when the value is not known.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import toml

from dango.utils.env_file import parse_env_file

REDACTED = "[REDACTED]"

# Known values shorter than this are not matched: replacing 3-5 character values would
# shred ordinary words in the log text ("true", "prod", "1234").
MIN_SECRET_LENGTH = 6

SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)"
)
_SECRET_INLINE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization)[\w-]*)"
    r"([\"']?\s*[=:]\s*)((?:Bearer|Basic|Token)\s+\S+|\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_URL_USERINFO_RE = re.compile(r"(://)[^/\s@]+@")
_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)(\s+)[A-Za-z0-9._~+/=-]{6,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?(?:[A-Za-z]+\s+)?)[^\s\"',;]+")
_QUERY_PARAM_RE = re.compile(
    r"(?i)([?&](?:token|key|secret|password|api_key|access_token|signature)=)[^&\s\"'#]+"
)
_WEBHOOK_RE = re.compile(
    r"(?i)(https://hooks\.slack\.com/(?:services|workflows|triggers)/|"
    r"https://(?:discord(?:app)?\.com)/api/(?:v\d+/)?webhooks/)[^\s\"'<>]+"
)

_cache: dict[Path, tuple[tuple[int, int], frozenset[str]]] = {}


def redact_patterns(text: str) -> str:
    """Mask secret-looking patterns in free text without knowing the secret values."""
    text = _SECRET_INLINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)
    text = _AUTH_HEADER_RE.sub(r"\1****", text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    text = _QUERY_PARAM_RE.sub(rf"\1{REDACTED}", text)
    text = _WEBHOOK_RE.sub(rf"\1{REDACTED}", text)
    return _BEARER_RE.sub(r"\1\2****", text)


def redact_text(text: str, secrets: frozenset[str]) -> str:
    """Replace known secret values, URL userinfo, auth header values and token-like params.

    Known values shorter than ``MIN_SECRET_LENGTH`` are ignored. Longer values are replaced
    longest first so a value containing another one is removed whole.
    """
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= MIN_SECRET_LENGTH:
            text = text.replace(value, REDACTED)
    return redact_patterns(text)


def _collect_strings(node: Any, out: set[str]) -> None:
    if isinstance(node, str):
        out.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_strings(value, out)


def _stat_key(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (-1, -1)
    return (st.st_mtime_ns, st.st_size)


def _read_file_values(path: Path) -> frozenset[str]:
    try:
        if path.name == "secrets.toml":
            found: set[str] = set()
            _collect_strings(toml.loads(path.read_text(encoding="utf-8")), found)
        else:
            found = set(parse_env_file(path.read_text(encoding="utf-8")).values())
    except Exception:
        return frozenset()
    return frozenset(v for v in found if len(v) >= MIN_SECRET_LENGTH)


def load_secret_values(project_root: Path) -> frozenset[str]:
    """Values (length >= 6) from ``.env`` and ``.dlt/secrets.toml``; never raises.

    Results are cached per file and invalidated when its mtime or size changes, so a log
    request does not re-parse unchanged files. An unreadable or malformed file contributes
    nothing; the other file is still used.
    """
    values: set[str] = set()
    for path in (project_root / ".env", project_root / ".dlt" / "secrets.toml"):
        try:
            key = _stat_key(path)
            cached = _cache.get(path)
            if cached is None or cached[0] != key:
                cached = (key, _read_file_values(path) if key != (-1, -1) else frozenset())
                _cache[path] = cached
            values |= cached[1]
        except Exception:
            continue
    return frozenset(values)
