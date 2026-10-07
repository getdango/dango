"""dango/platform/cloud/server_auth.py

Keep the cloud auth timeouts in the ``.dango/project.yml`` uploaded to a server.

Provisioning sets ``auth.session_max_days: 30`` and ``auth.idle_timeout_minutes: 60``
on the server.  The local file normally omits both keys (local defaults are
365 days / 24 h idle), so uploading it verbatim on ``dango remote push`` would
silently revert the server to the local defaults.  The uploaded copy therefore
gets the cloud values for every key the local file does not set explicitly;
an explicit local value wins.  The local file is never touched.

The edit is a targeted text insertion so comments, ordering, other keys and the
file's line endings survive.  If the ``auth`` section uses a form the insertion
cannot reach (e.g. flow style ``auth: {enabled: true}``), the content is
re-dumped through PyYAML as a fallback (comments are lost in that case).
"""

from __future__ import annotations

import re

import yaml

from dango.logging import get_logger

_logger = get_logger(__name__)

#: Cloud values (must match ``_build_auth_timeout_script`` in ``deploy_provision``).
CLOUD_AUTH_TIMEOUTS: dict[str, int] = {"session_max_days": 30, "idle_timeout_minutes": 60}

_AUTH_RE = re.compile(r"^auth\s*:\s*(#.*)?$")
_AUTH_EMPTY_RE = re.compile(r"^auth\s*:\s*(\{\s*\}|~|null)\s*(#.*)?$")
_CHILD_RE = re.compile(r"^(?P<indent>[ \t]+)(?P<key>[^\s#:][^:]*?)\s*:(\s|$)")


def _fallback(text: str) -> str:
    """Re-dump through PyYAML with the cloud timeouts filled in (comments lost)."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return text
    auth = data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    for key, value in CLOUD_AUTH_TIMEOUTS.items():
        if auth.get(key) is None:
            auth[key] = value
    data["auth"] = auth
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


def apply_cloud_auth_timeouts(text: str) -> str:
    """Return *text* with the cloud auth timeouts set where not set explicitly."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return text
    auth = data.get("auth")
    if auth is not None and not isinstance(auth, dict):
        return text
    present = auth or {}
    missing = [k for k in CLOUD_AUTH_TIMEOUTS if k not in present]
    if not missing and all(present[k] is not None for k in CLOUD_AUTH_TIMEOUTS):
        return text
    if any(k in present and present[k] is None for k in CLOUD_AUTH_TIMEOUTS):
        # ``session_max_days:`` with no value fails AuthConfig validation, and the web app
        # then silently uses the 365 d / 24 h defaults; fill it (re-dump, comments lost).
        return _fallback(text)

    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _AUTH_RE.match(ln)), None)
    empty = next((i for i, ln in enumerate(lines) if _AUTH_EMPTY_RE.match(ln)), None)

    if start is None and empty is None and "auth" in data:
        return _fallback(text)  # flow style or other unreachable form

    indent = "  "
    if start is None and empty is None:  # no auth section: append one
        new = ["auth:"]
        insert_at = len(lines)
    elif start is None:  # ``auth: {}`` / ``auth: ~``: replace with a block
        assert empty is not None
        lines[empty] = "auth:"
        new = []
        insert_at = empty + 1
    else:
        # Block runs while lines are blank, comments or indented.
        end = start
        seen_child = False
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if not ln.strip() or ln.lstrip().startswith("#") and ln[:1] in " \t":
                continue
            if ln[:1] in " \t":
                end = i
                m = _CHILD_RE.match(ln)
                if m and not seen_child:
                    indent = m.group("indent")
                    seen_child = True
                continue
            break
        new = []
        insert_at = end + 1
    new += [f"{indent}{k}: {CLOUD_AUTH_TIMEOUTS[k]}" for k in missing]
    lines[insert_at:insert_at] = new
    result = nl.join(lines) + nl

    # Safety net: the edit must parse to the intended values, else re-dump.
    try:
        check = yaml.safe_load(result)
        got = check.get("auth", {}) if isinstance(check, dict) else {}
        if isinstance(got, dict) and all(k in got for k in CLOUD_AUTH_TIMEOUTS):
            return result
    except yaml.YAMLError:
        pass
    return _fallback(text)


def apply_cloud_auth_timeouts_bytes(raw: bytes) -> bytes | None:
    """Byte-level wrapper used by the uploader.

    Returns ``None`` when nothing needs changing (upload the original bytes) or
    when *raw* is not valid UTF-8 (uploaded unmodified; logged).  A UTF-8 BOM is kept.
    """
    bom = b"\xef\xbb\xbf"
    body = raw[len(bom) :] if raw.startswith(bom) else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        _logger.warning("server_auth_timeouts_skipped_non_utf8")
        return None
    new_text = apply_cloud_auth_timeouts(text)
    if new_text == text:
        return None
    return (bom if raw.startswith(bom) else b"") + new_text.encode("utf-8")
