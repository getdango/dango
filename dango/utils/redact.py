"""dango/utils/redact.py

Shared secret redaction for log text served by the web API and the MCP tools.

Level 0 module (standard library, ``toml``, ``dango.utils.env_file`` and ``dango.utils.dlt_error_text`` only; ``tomllib`` is 3.11+). Exact known secret values are replaced with
``[REDACTED]``; secret-looking patterns (key=value pairs, URL credentials, auth headers,
signed query parameters, chat webhook URLs) are masked even when the value is not known.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import toml

from dango.utils.dlt_error_text import rewrite_dlt_advice
from dango.utils.env_file import parse_env_file

REDACTED = "[REDACTED]"

# Known values shorter than this are not matched: replacing 3-5 character values would
# shred ordinary words in the log text ("true", "prod", "1234").
MIN_SECRET_LENGTH = 6

SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)"
)
# Matching starts at the keyword (the text before it is kept as is, so it needs no capture) and
# the trailing name part is bounded. The old ``(?<!..)[\w-]*keyword[\w-]*`` form was quadratic on
# long runs of word characters: every ``_`` restarted a scan of the whole run.
_SECRET_INLINE_RE = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)[\w-]{0,64})"
    r"([\"']?\s*[=:]\s*)(?![\"']?-----BEGIN )((?:Bearer|Basic|Token)\s+\S+|\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_URL_USERINFO_RE = re.compile(r"(://)[^/\s@]+@")
_BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic)(\s+)[A-Za-z0-9._~+/=-]{6,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?(?:[A-Za-z]+\s+)?)[^\s\"',;]+")
_QUERY_PARAM_RE = re.compile(
    r"(?i)((?:[?&]|%3F|%26)(?:token|key|secret|password|api_key|access_token|signature|sig|auth|sas"
    r"|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential)"
    r"(?:=|%3D))(?:(?!%26)[^&\s\"'#])+"
)
# A PEM private key (also the JSON form with escaped newlines, or cut off before the END line).
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*"
    r"(?:-----END [A-Z ]{0,20}PRIVATE KEY-----)?"
)
_AIRTABLE_PAT_RE = re.compile(r"\bpat[A-Za-z0-9]{14}\.[A-Fa-f0-9]{64}\b")
_WEBHOOK_RE = re.compile(
    r"(?i)(https://hooks\.slack\.com/(?:services|workflows|triggers)/|"
    r"https://(?:discord(?:app)?\.com)/api/(?:v\d+/)?webhooks/)[^\s\"'<>]+"
)

_cache: dict[Path, tuple[tuple[int, int], frozenset[str]]] = {}


def redact_patterns(text: str) -> str:
    """Mask secret-looking patterns in free text without knowing the secret values."""
    # The inline pattern skips a value that starts a key block (lookahead), so the block is left
    # whole for the PEM pass below. Running the PEM pass first would let a cut-off block (no END
    # line) swallow a following ``api_key=`` line and hide its name from this pattern.
    text = _SECRET_INLINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)
    text = _PEM_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(r"\1****", text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    text = _QUERY_PARAM_RE.sub(rf"\1{REDACTED}", text)
    text = _AIRTABLE_PAT_RE.sub(REDACTED, text)
    text = _WEBHOOK_RE.sub(rf"\1{REDACTED}", text)
    return _BEARER_RE.sub(r"\1\2****", text)


def redact_text(text: str, secrets: frozenset[str]) -> str:
    """Replace known secret values, URL userinfo, auth header values and token-like params.

    dlt's command-line advice after a failed load is replaced by Dango's wording too: every
    error text that is stored, printed or sent passes through here.

    Known values shorter than ``MIN_SECRET_LENGTH`` are ignored. Longer values are replaced
    longest first so a value containing another one is removed whole.
    """
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= MIN_SECRET_LENGTH:
            text = text.replace(value, REDACTED)
    # last, so the raw passage that reaches the debug log is already redacted
    return rewrite_dlt_advice(redact_patterns(text))


def redact_nested(value: Any, secrets: frozenset[str]) -> Any:
    """Apply ``redact_text`` to every string in *value*, including nested dicts and lists."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: redact_nested(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_nested(v, secrets) for v in value]
    return value


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


def redact_project_text(project_root: Path | None, text: str) -> str:
    """Redact *text* with the project's known secret values; patterns only without a root.

    For error text that is stored or sent to other roles (sync history, schedule records,
    WebSocket events): redact it once where it is built.
    """
    secrets = load_secret_values(project_root) if project_root is not None else frozenset()
    return redact_text(text, secrets)


def redact_error_fields(project_root: Path | None, record: dict[str, Any]) -> dict[str, Any]:
    """A copy of *record* with every value whose key contains ``error`` redacted."""
    secrets = load_secret_values(project_root) if project_root is not None else frozenset()
    return {
        key: redact_nested(value, secrets) if "error" in key.lower() else value
        for key, value in record.items()
    }
