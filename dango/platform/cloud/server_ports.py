"""dango/platform/cloud/server_ports.py

Normalize the platform ports in the ``.dango/project.yml`` that is uploaded
to a server.

A laptop may use custom ports (``platform.port: 8861``) to avoid a local
conflict, but the server's Caddyfile always proxies to the standard ports
(web 8800, marimo 7805).  Uploading the local file verbatim makes Dango web
listen on a port Caddy does not proxy to (HTTP 502).  The uploaded copy is
therefore rewritten to the standard server ports; the local file is never
touched.

The edit is a targeted text substitution of the four scalar keys (direct
children of ``platform:`` only) so comments, ordering, line endings and every
other key survive.  If a key uses a form the substitution cannot reach (e.g. a
flow-style ``platform: {port: 1}``), the content is re-dumped through PyYAML
as a fallback (comments are lost in that case).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from dango.config.models import PlatformSettings
from dango.logging import get_logger

_logger = get_logger(__name__)

#: ``platform:`` keys that must hold the standard server value on a server.
SERVER_PORT_KEYS: tuple[str, ...] = ("port", "metabase_port", "dbt_docs_port", "marimo_port")

_PLATFORM_RE = re.compile(r"^platform\s*:\s*(#.*)?$")
_INDENT_RE = re.compile(r"^(?P<indent>[ \t]+)\S")
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<key>port|metabase_port|dbt_docs_port|marimo_port)"
    r"(?P<sep>\s*:\s*)(?P<value>'[^']*'|\"[^\"]*\"|[^\s#'\"]+)(?P<tail>\s*(?:#.*)?)$"
)


@dataclass(frozen=True)
class PortChange:
    """One port key rewritten in the uploaded copy (``old`` is None if not a valid port)."""

    key: str
    old: int | None
    new: int


def server_port_defaults() -> dict[str, int]:
    """Standard server ports, read from the ``PlatformSettings`` field defaults."""
    return {key: int(PlatformSettings.model_fields[key].default) for key in SERVER_PORT_KEYS}


def _as_port(value: Any) -> int | None:
    """Coerce a YAML scalar to an int port, or ``None`` if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _non_standard(text: str, defaults: dict[str, int]) -> list[PortChange]:
    """Return the keys in *text* whose value is not the standard one (after int coercion)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    platform = data.get("platform") if isinstance(data, dict) else None
    if not isinstance(platform, dict):
        return []
    changes: list[PortChange] = []
    for key in SERVER_PORT_KEYS:
        if key in platform:
            old = _as_port(platform[key])
            if old != defaults[key]:
                changes.append(PortChange(key, old, defaults[key]))
    return changes


def normalize_server_ports(text: str) -> tuple[str, list[PortChange]]:
    """Return *text* with the platform ports set to the standard server values.

    Keys that are absent are left absent (the implied default is already the
    standard value).  When nothing needs changing the input is returned
    unchanged (same string), so its bytes and hash are identical.
    """
    defaults = server_port_defaults()
    if not _non_standard(text, defaults):
        return text, []

    lines = text.splitlines(keepends=True)
    changes: list[PortChange] = []
    in_platform = False
    child_indent: str | None = None
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body) :]
        if not in_platform:
            in_platform = bool(_PLATFORM_RE.match(body))
            child_indent = None
            continue
        if body and not body[0].isspace() and not body.startswith("#"):
            in_platform = bool(_PLATFORM_RE.match(body))
            child_indent = None
            continue
        indent_match = _INDENT_RE.match(body)
        if indent_match is None or body.lstrip().startswith("#"):
            continue
        if child_indent is None:
            child_indent = indent_match["indent"]
        if indent_match["indent"] != child_indent:
            continue  # nested deeper (or dedented oddly): not a direct child
        match = _KEY_RE.match(body)
        if match is None:
            continue
        key = match["key"]
        raw = match["value"]
        old = _as_port(yaml.safe_load(raw))
        if old == defaults[key]:
            continue
        lines[i] = f"{match['indent']}{key}{match['sep']}{defaults[key]}{match['tail']}{eol}"
        changes.append(PortChange(key, old, defaults[key]))

    new_text = "".join(lines)
    leftover = _non_standard(new_text, defaults)
    if leftover:
        # Unusual formatting (flow style, anchors, ...): fall back to a dump.
        _logger.warning("server_ports_text_edit_incomplete", keys=[c.key for c in leftover])
        data = yaml.safe_load(new_text)
        for change in leftover:
            data["platform"][change.key] = defaults[change.key]
            changes.append(change)
        new_text = yaml.dump(data, default_flow_style=False, sort_keys=False)
    return new_text, changes


def normalize_server_ports_bytes(raw: bytes) -> tuple[bytes | None, list[PortChange]]:
    """Byte-level wrapper used by the uploader.

    Returns ``(None, [])`` when nothing needs changing (upload the original
    bytes) or when *raw* is not valid UTF-8 (upload unmodified; logged).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _logger.warning("server_ports_skipped_non_utf8")
        return None, []
    new_text, changes = normalize_server_ports(text)
    if not changes:
        return None, []
    return new_text.encode("utf-8"), changes


def format_port_notice(changes: list[PortChange]) -> str | None:
    """User-facing one-line notice, or ``None`` when nothing was changed."""
    if not changes:
        return None
    defaults = server_port_defaults()
    standard = (
        f"web {defaults['port']}, metabase {defaults['metabase_port']}, "
        f"dbt docs {defaults['dbt_docs_port']}, marimo {defaults['marimo_port']}"
    )
    changed = ", ".join(
        f"{c.key} {c.old} -> {c.new}" if c.old is not None else f"{c.key} invalid value -> {c.new}"
        for c in changes
    )
    return (
        f"Using the standard server ports ({standard}); your local ports are unchanged. "
        f"Changed in the uploaded copy: {changed}."
    )
