"""dango/platform/cloud/isolation_audit.py

Who and when for ``dango remote isolation disable|enable``.

* the one-line record written into the root-owned opt-out marker (informational: it is never
  trusted for a decision, only shown),
* the audit event written on the server by the service account (a unique nonce is read back so a
  stale identical line cannot pass),
* the actor label: the local login plus the SSH key fingerprint and target.

Everything here is a pure builder: nothing runs a command.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from dango.auth.audit import AuditEvent
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.run_as import PYTHON

__all__ = [
    "AUDIT_LOG_DIR",
    "AUDIT_SOURCE",
    "audit_command",
    "clean_text",
    "journal_command",
    "marker_prefix",
    "ssh_actor",
]

#: Directory of the project audit log on a server (owned by the service account).
AUDIT_LOG_DIR = "/srv/dango/project/.dango/logs"
_FIELD_MAX = 120
#: Run by the service account.  ``log_auth_event`` swallows write errors, so the log tail is read
#: back and must hold this call's nonce: a missing entry (or a stale identical one) is non-zero.
AUDIT_SOURCE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from dango.auth.audit import AuditEvent, log_auth_event\n"
    "event, actor, server, nonce = sys.argv[1:5]\n"
    f"log_dir = Path({AUDIT_LOG_DIR!r})\n"
    "log_auth_event(\n"
    "    AuditEvent(event),\n"
    "    details={'actor': actor, 'server': server, 'nonce': nonce,\n"
    "             'source': 'dango remote isolation'},\n"
    "    log_dir=log_dir,\n"
    ")\n"
    "with open(log_dir / 'audit.jsonl', 'rb') as fh:\n"
    "    fh.seek(0, 2)\n"
    "    fh.seek(max(0, fh.tell() - 16384))\n"
    "    tail = fh.read().decode('utf-8', 'replace').splitlines()\n"
    "ok = False\n"
    "for line in tail[-100:]:\n"
    "    try:\n"
    "        entry = json.loads(line)\n"
    "    except ValueError:\n"
    "        continue\n"
    "    details = entry.get('details') if isinstance(entry, dict) else None\n"
    "    if isinstance(details, dict) and details.get('nonce') == nonce:\n"
    "        ok = entry.get('event') == event\n"
    "sys.exit(0 if ok else 1)\n"
)


def clean_text(value: object, limit: int = _FIELD_MAX) -> str:
    """*value* as one printable ASCII line of at most *limit* characters (no control characters)."""
    return "".join(ch for ch in str(value) if ch.isascii() and ch.isprintable())[:limit].strip()


def audit_command(event: AuditEvent, actor: str, server: str, nonce: str) -> str:
    """Shell command (for a root session) that writes *event* as the service account.

    Fixed source, fixed interpreter, ``-I`` (no environment or cwd imports), arguments quoted;
    no secrets are passed.
    """
    return (
        f"cd / && runuser -u {scripts.SERVICE_USER} -- {PYTHON} -I -c {shlex.quote(AUDIT_SOURCE)} "
        f"{shlex.quote(event.value)} {shlex.quote(actor)} {shlex.quote(server)} "
        f"{shlex.quote(nonce)}"
    )


def marker_prefix(by: str, at: str, server: str) -> str:
    """The marker's JSON text without its closing brace (the script appends the SSH client).

    One ASCII line: fields are cleaned and bounded, and JSON escapes the rest.
    """
    record = {"schema": 1, "by": clean_text(by), "at": clean_text(at), "server": clean_text(server)}
    return json.dumps(record)[:-1]


def journal_command(event: AuditEvent, by: str, at: str, server: str) -> str:
    """Best-effort ``logger`` call: a copy of the record in the journal, never a failure."""
    message = f"{event.value} by={clean_text(by)} at={clean_text(at)} server={clean_text(server)}"
    return (
        "{ command -v logger >/dev/null 2>&1 && "
        f"logger -t dango-isolation -p auth.notice -- {shlex.quote(message)}; "
        "} >/dev/null 2>&1 || true\n"
    )


def _fingerprint(public_key_file: Path) -> str | None:
    """``SHA256:...`` of the first key in a ``.pub`` file, or None."""
    try:
        parts = public_key_file.read_text(encoding="utf-8").split()
        digest = hashlib.sha256(base64.b64decode(parts[1], validate=True)).digest()
    except (OSError, IndexError, ValueError):
        return None
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def ssh_actor(cloud_cfg: Any, project_root: Path, ip: str, local_user: str) -> str:
    """Label for the audit trail: login name, SSH key fingerprint (if known) and SSH target."""
    who = clean_text(local_user) or "unknown"
    key = getattr(cloud_cfg, "ssh_key_path", None)
    fingerprint = None
    if isinstance(key, str) and key:
        path = Path(key) if Path(key).is_absolute() else project_root / key
        fingerprint = _fingerprint(path.with_name(path.name + ".pub"))
    key_part = f" (key {fingerprint})" if fingerprint else ""
    return f"{who}{key_part} via ssh root@{clean_text(ip)}"
