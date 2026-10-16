"""dango/transformation/model_docs_guard.py

Fail-closed check of agent/service-written dbt docs: no Jinja, only built-in tests with SQL-free arguments.
"""

import re
from typing import Any

_HAND_EDIT = "edit the schema yml by hand (it is reviewed in git)"
_JINJA_TOKENS = ("{{", "{%", "{#")
_COLUMN_KEYS = {"name", "description", "data_type", "data_tests", "tests", "meta", "tags", "quote"}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATA_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?(\[\])?")
_Q = r"(['\"])"
_REL_TO_RE = re.compile(
    rf"\s*(?:ref\(\s*{_Q}[A-Za-z0-9_\-]+\1\s*\)"
    rf"|source\(\s*{_Q}[A-Za-z0-9_.\-]+\2\s*,\s*{_Q}[A-Za-z0-9_.\-]+\3\s*\))\s*"
)
_TEST_ARGS = {
    "unique": set(),
    "not_null": set(),
    "accepted_values": {"values", "quote"},
    "relationships": {"to", "field"},
}


def _jinja_errors(value: Any, where: str) -> list[str]:
    """Any string (key or value, recursively) holding a Jinja delimiter is rejected."""
    if isinstance(value, str):
        if any(t in value for t in _JINJA_TOKENS):
            return [
                f"{where}: Jinja ({{{{ }}}}, {{% %}}, {{# #}}) is not allowed in agent-written docs"
            ]
        return []
    if isinstance(value, dict):
        return [
            e for k, v in value.items() for e in _jinja_errors(k, where) + _jinja_errors(v, where)
        ]
    if isinstance(value, (list, tuple)):
        return [e for v in value for e in _jinja_errors(v, where)]
    return []


def _check_config(cfg: Any, where: str) -> list[str]:
    if not isinstance(cfg, dict):
        return [f"{where}: test config must be a mapping"]
    errors: list[str] = []
    for key, val in cfg.items():
        ok = (
            (key == "severity" and str(val).lower() in ("warn", "error"))
            or (key == "enabled" and isinstance(val, bool))
            or (key == "tags" and isinstance(val, list) and all(isinstance(t, str) for t in val))
        )
        if not ok:
            errors.append(
                f"{where}: test config '{key}' is not allowed (only severity, enabled, tags); {_HAND_EDIT}"
            )
    return errors


def _check_args(test: str, args: dict[str, Any], where: str) -> list[str]:
    errors: list[str] = []
    for key, val in args.items():
        if key not in _TEST_ARGS[test]:
            errors.append(f"{where}: argument '{key}' of {test} is not allowed; {_HAND_EDIT}")
        elif key == "quote" and val is not True:
            errors.append(f"{where}: accepted_values quote must be true")
        elif key == "values" and not (
            isinstance(val, list)
            and all(
                isinstance(v, (int, float, bool))
                or (isinstance(v, str) and not set(v) & {"'", "\\"})
                for v in val
            )
        ):
            errors.append(f"{where}: accepted_values values must be plain literals without quotes")
        elif key == "to" and not (isinstance(val, str) and _REL_TO_RE.fullmatch(val)):
            errors.append(
                f"{where}: relationships 'to' must be ref('model') or source('src', 'table')"
            )
        elif key == "field" and not (isinstance(val, str) and _IDENT_RE.fullmatch(val)):
            errors.append(f"{where}: relationships 'field' must be a column name")
    return errors


def _check_test(item: Any, where: str) -> list[str]:
    if isinstance(item, str):
        if item in ("unique", "not_null"):
            return []
        return [f"{where}: test '{item}' is not allowed (built-in tests only); {_HAND_EDIT}"]
    if not (isinstance(item, dict) and len(item) == 1):
        return [f"{where}: each test must be a name or a single-key mapping"]
    ((test, body),) = item.items()
    if test not in _TEST_ARGS:
        return [f"{where}: test '{test}' is not allowed (built-in tests only); {_HAND_EDIT}"]
    if body is None:
        return []
    if not isinstance(body, dict):
        return [f"{where}: arguments of {test} must be a mapping"]
    errors: list[str] = []
    args = {k: v for k, v in body.items() if k not in ("config", "arguments")}
    if "arguments" in body:
        if not isinstance(body["arguments"], dict):
            return [f"{where}: arguments of {test} must be a mapping"]
        args.update(body["arguments"])
    if "config" in body:
        errors += _check_config(body["config"], where)
    return errors + _check_args(test, args, where)


def _check_column(col: dict[str, Any]) -> list[str]:
    where = f"column '{col.get('name')}'"
    errors: list[str] = []
    for key, val in col.items():
        if key not in _COLUMN_KEYS:
            errors.append(f"{where}: key '{key}' is not allowed; {_HAND_EDIT}")
        elif key == "data_type" and not (isinstance(val, str) and _DATA_TYPE_RE.fullmatch(val)):
            errors.append(f"{where}: data_type must be a plain type name")
        elif key == "quote" and not isinstance(val, bool):
            errors.append(f"{where}: quote must be true or false")
        elif key in ("data_tests", "tests"):
            if not isinstance(val, list):
                errors.append(f"{where}: {key} must be a list")
            else:
                errors += [e for item in val for e in _check_test(item, where)]
    return errors


def check_docs_input(description: Any, columns: list[dict[str, Any]] | None) -> list[str]:
    """Return errors for a model/source description and column entries written by an agent."""
    errors = _jinja_errors(description, "description")
    for col in columns or []:
        if isinstance(col, dict):
            col_errors = _jinja_errors(col, f"column '{col.get('name')}'")
            errors += col_errors or _check_column(col)
    return errors
