"""dango/transformation/model_sql_functions.py

Function allow-list for agent-written model SQL: DuckDB catalog functions minus file/settings/env access.
"""

import re
from functools import lru_cache
from importlib import resources
from typing import Any

ALLOWLIST_FILE = "duckdb_function_allowlist.txt"
_EXTEND = "write the model by hand or ask to extend the allow-list"

DENIED_FUNCS = frozenset(
    "getenv current_setting getvariable query query_table sniff_csv glob which_secret "
    "write_log pg_sleep sleep_ms nextval currval setseed get_block_size pg_get_viewdef".split()
)
# json_serialize*/json_deserialize*/json_execute* parse, plan or run SQL given as a string
DENIED_PREFIXES = (
    "read_",
    "parquet_",
    "pragma_",
    "duckdb_",
    "json_serialize",
    "json_deserialize",
    "json_execute",
)
DENIED_SUFFIXES = ("_scan", "_attach")

# Function-call syntax DuckDB resolves in its parser/binder, so it is not listed in
# duckdb_functions() (special forms, window-only functions, star expressions).
SYNTAX_FUNCS = frozenset(
    "cast try_cast extract coalesce if iif ifnull nvl exists grouping grouping_id columns "
    "unpack unnest struct row array list map interval case".split()
)

_CALL_HEAD_RE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def is_denied_function(name: str) -> bool:
    """True for functions that read files/URLs, settings, variables, secrets or env, or have side effects."""
    name = name.lower()
    return (
        name in DENIED_FUNCS or name.startswith(DENIED_PREFIXES) or name.endswith(DENIED_SUFFIXES)
    )


@lru_cache(maxsize=1)
def load_function_allowlist() -> frozenset[str]:
    """Function names from the shipped allow-list file (comments and blanks ignored)."""
    text = resources.files("dango.transformation").joinpath(ALLOWLIST_FILE).read_text("utf-8")
    return frozenset(
        ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")
    )


def function_name(node: Any) -> str | None:
    """DuckDB name of a sqlglot function node; None when it renders as plain syntax."""
    from sqlglot import exp

    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    m = _CALL_HEAD_RE.match(node.sql(dialect="duckdb"))
    return m.group(1).lower() if m else None


def function_error(node: Any, next_step: str) -> str | None:
    """Error for a function node that is denied or not on the allow-list, else None."""
    name = function_name(node)
    if name is None:
        return None
    if is_denied_function(name):
        return f"{name}() is not allowed in agent-created models. {next_step}"
    if name not in SYNTAX_FUNCS and name not in load_function_allowlist():
        return f"function {name}() is not allowed in agent-created models; {_EXTEND}"
    return None
