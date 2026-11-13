"""dango/utils/dbt_escaping.py

Escaping helpers for text that Dango generates into dbt SQL and YAML files.
dbt renders model SQL (comments included) and YAML names/descriptions as Jinja,
so data-derived strings (column and table names, descriptions) must never reach
those files with live ``{{``, ``{%`` or ``{#`` delimiters.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

import duckdb

# A "{" that would open (or, at end of string, could combine with following text
# into) a Jinja delimiter.
_JINJA_OPEN = re.compile(r"\{(?=[{%#]|$)")
_PLAIN_YAML = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_YAML_RESERVED_WORDS = frozenset({"y", "n", "yes", "no", "true", "false", "on", "off", "null"})
_LINE_BREAKS = re.compile(r"[\r\n\x85\u2028\u2029]+")
# C0/C1 control characters (except tab and newline) are not allowed in YAML text.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Fallback if DuckDB's keyword list cannot be read: every non-"unreserved" word from
# duckdb_keywords() on DuckDB 1.5.4 (regenerate when the pinned DuckDB changes).
FALLBACK_KEYWORDS = frozenset(
    """
    all analyse analyze and anti any array as asc asof asymmetric at authorization
    between bigint binary bit boolean both by case cast char character check coalesce
    collate collation column columns concurrently constraint create cross dec decimal
    default deferrable desc describe distinct do else end except exists extract false
    fetch float for foreign freeze from full generated glob group grouping grouping_id
    having ilike in initially inner inout int integer intersect interval into is isnull
    join lambda lateral leading left like limit map national natural nchar none not
    notnull null nullif numeric offset on only or order out outer overlaps overlay pivot
    pivot_longer pivot_wider placing position positional precision primary qualify real
    references returning right row select semi setof show similar smallint some struct
    substring summarize symmetric table tablesample then time timestamp to trailing
    treat trim true try_cast union unique unpack unpivot using values varchar variadic
    verbose when where window with xmlattributes xmlconcat xmlelement xmlexists
    xmlforest xmlnamespaces xmlparse xmlpi xmlroot xmlserialize xmltable
    """.split()
)


def neutralize_jinja(value: str) -> str:
    """Return ``value`` with every Jinja-opening brace replaced by ``{{ '{' }}``.

    dbt renders the result back to the original text, so the literal survives in
    manifests and warehouse identifiers, but nothing in it is ever evaluated.
    """
    return _JINJA_OPEN.sub("{{ '{' }}", value)


def one_line(value: str) -> str:
    """Collapse line breaks to spaces and replace control characters with U+FFFD.

    For SQL comments and YAML block scalars, where neither may appear raw.
    """
    return _CONTROL_CHARS.sub("\ufffd", _LINE_BREAKS.sub(" ", value))


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
    quoted = quoted.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return re.sub(r"[\x7f-\x9f]", lambda m: f"\\x{ord(m.group()):02x}", quoted)


@lru_cache(maxsize=1)
def _quoted_keywords() -> frozenset[str]:
    """Lower-case DuckDB keywords that are not safe as bare column names.

    Everything except the ``unreserved`` category is quoted (``reserved``,
    ``type_function`` and ``column_name`` words fail or change meaning unquoted
    in a select list). Read from the installed DuckDB so it tracks the version.
    """
    try:
        conn = duckdb.connect(":memory:")
        try:
            rows = conn.execute(
                "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category <> 'unreserved'"
            ).fetchall()
        finally:
            conn.close()
        words = frozenset(str(row[0]).lower() for row in rows)
        return words or FALLBACK_KEYWORDS
    except Exception:
        return FALLBACK_KEYWORDS


def needs_identifier_quoting(name: str) -> bool:
    """True if ``name`` must be double-quoted as a SQL column identifier."""
    return not (
        name
        and all(c.isalnum() or c == "_" for c in name)
        and not name[0].isdigit()
        and name.lower() not in _quoted_keywords()
    )
