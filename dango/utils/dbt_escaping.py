"""dango/utils/dbt_escaping.py

Escaping helpers for text that Dango generates into dbt SQL and YAML files.
dbt renders model SQL (comments included) and YAML names/descriptions as Jinja,
so data-derived strings (column and table names, descriptions) must never reach
those files with live ``{{``, ``{%`` or ``{#`` delimiters.
"""

from __future__ import annotations

import json
import re

# A "{" that would open (or, at end of string, could combine with following text
# into) a Jinja delimiter.
_JINJA_OPEN = re.compile(r"\{(?=[{%#]|$)")
_PLAIN_YAML = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_YAML_RESERVED_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})
_LINE_BREAKS = re.compile(r"[\r\n\x85\u2028\u2029]+")


def neutralize_jinja(value: str) -> str:
    """Return ``value`` with every Jinja-opening brace replaced by ``{{ '{' }}``.

    dbt renders the result back to the original text, so the literal survives in
    manifests and warehouse identifiers, but nothing in it is ever evaluated.
    """
    return _JINJA_OPEN.sub("{{ '{' }}", value)


def one_line(value: str) -> str:
    """Collapse line breaks to single spaces (for SQL comments and YAML block scalars)."""
    return _LINE_BREAKS.sub(" ", value)


def comment_text(value: object) -> str:
    """Neutralise ``value`` and keep it on one line, for ``--`` SQL comments and block text."""
    return neutralize_jinja(one_line(str(value)))


def jinja_string_literal(value: object) -> str:
    """Return ``value`` as a quoted Jinja string literal for ``source()`` arguments.

    dbt reads the arguments of ``source()``/``ref()`` without unescaping
    backslashes, so a quote character is avoided rather than escaped whenever
    possible: single quotes are used unless the value contains one, then double
    quotes. A value containing both kinds of quote (or a backslash or line break)
    is backslash-escaped, which Jinja renders correctly but dbt may not resolve.
    """
    text = str(value)
    plain = "\\" not in text and "\n" not in text and "\r" not in text
    if plain and "'" not in text:
        return "'" + text + "'"
    if plain and '"' not in text:
        return '"' + text + '"'
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped.replace("\r", "\\r").replace("\n", "\\n") + "'"


def yaml_scalar(value: object) -> str:
    """Render ``value`` as a YAML scalar that is safe from Jinja rendering.

    Plain identifiers stay unquoted; everything else becomes a double-quoted
    scalar (JSON escaping is valid YAML) with Jinja delimiters neutralised.
    """
    text = str(value)
    if _PLAIN_YAML.fullmatch(text) and text.lower() not in _YAML_RESERVED_WORDS:
        return text
    quoted = json.dumps(neutralize_jinja(text), ensure_ascii=False)
    return quoted.replace("\x85", "\\x85").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
