"""dango/ingestion/sources/setup_schema.py

Source setup types, setup schema, and parameter normalisation shared by the source wizard and MCP.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from dango.ingestion.sources.registry import AuthType, get_source_metadata

MCP_UNSUPPORTED_SOURCE_TYPES = frozenset({"rest_api", "dlt_native"})

# Source types whose sync can fully replace a table (either always, like CSV/Local Files, or as
# the common default of dlt's write_disposition — verified 2026-09-27 against
# dango/ingestion/dlt_sources/*/__init__.py's write_disposition="replace" resources, plus
# Postgres/MySQL's dlt sql_database default). Sources not in this set are merge/append-only and
# never see the empty-sync-policy prompt. Members must be registry keys (not dlt package names).
REPLACE_MODE_SOURCE_TYPES = frozenset(
    {
        "csv",
        "local_files",
        "postgres",
        "mysql",
        "airtable",
        "asana",
        "chess",
        "facebook_ads",
        "github",
        "google_analytics",
        "google_sheets",
        "hubspot",
        "jira",
        "mux",
        "notion",
        "personio",
        "pipedrive",
        "salesforce",
        "slack",
        "strapi",
        "stripe",
        "workable",
        "zendesk",
    }
)

_DAYS_AGO = re.compile(r"^\d+daysAgo$")
_BOOL_STRINGS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}


class SourceSetupError(Exception):
    """Validation failed; nothing was written."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class CredentialRequirement:
    """A credential the user must supply before the source can sync."""

    kind: str  # "env_var" | "oauth" | "secrets_toml"
    detail: str  # human instruction, e.g. "Set STRIPE_TEST_API_KEY in .env"
    name: str | None = None  # env var name for kind="env_var"
    command: str | None = None  # e.g. "dango oauth google_sheets" for kind="oauth"


@dataclass
class SourceSetupResult:
    """What create_source/apply_source wrote and what the user still has to do."""

    source_name: str
    source_type: str
    source_config: dict[str, Any]  # exactly what was appended to sources.yml
    files_changed: list[str] = field(default_factory=list)  # project-relative paths
    credentials_required: list[CredentialRequirement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)  # post-save validate_config()


def is_credential_param(param: dict[str, Any], source_type: str) -> bool:
    """Check if a parameter is a credential/secret that should be skipped when using OAuth

    Args:
        param: Parameter configuration from registry
        source_type: Source type key (e.g., "facebook_ads", "google_ads")
    """
    param_name = param.get("name", "").lower()
    param_type = param.get("type", "")

    # Check if it's a secret type
    if param_type == "secret":
        return True

    # Check common credential parameter name patterns
    credential_patterns = [
        "credentials",
        "credential",
        "access_token",
        "api_key",
        "secret",
        "_env",  # Parameters ending in _env are typically env var references
    ]

    for pattern in credential_patterns:
        if pattern in param_name:
            return True

    # Source-specific credential parameters that are collected during OAuth
    # These are stored in .dlt/secrets.toml by the OAuth provider
    oauth_collected_params: dict[str, list[str]] = {
        "facebook_ads": [],  # account_id is a required wizard param, not OAuth-collected
        "google_ads": [],  # customer_id is source-specific, not OAuth-collected
    }

    if source_type in oauth_collected_params:
        if param_name in oauth_collected_params[source_type]:
            return True

    return False


def compute_env_var_name(param: dict[str, Any], source_name: str) -> str:
    """Env var name for a secret param of a given source instance.

    Examples:
        slack + SLACK_ACCESS_TOKEN -> SLACK_ACCESS_TOKEN
        marketing_slack + SLACK_ACCESS_TOKEN -> MARKETING_SLACK_ACCESS_TOKEN
        stripe_test + STRIPE_API_KEY -> STRIPE_TEST_API_KEY
    """
    base_env_var = param.get("env_var", param["name"].upper())
    if "_" in base_env_var:
        suffix = "_".join(base_env_var.split("_")[1:])
        source_prefix = source_name.upper().replace("-", "_")
        return f"{source_prefix}_{suffix}"
    return f"{base_env_var}_{source_name.upper().replace('-', '_')}"


def _is_secret_param(param: dict[str, Any]) -> bool:
    return param.get("type") == "secret" or str(param.get("name", "")).endswith("_env")


def _managed_by(param: dict[str, Any], metadata: dict[str, Any], source_type: str) -> str:
    if metadata.get("auth_type") == AuthType.OAUTH and is_credential_param(param, source_type):
        return "oauth"
    if _is_secret_param(param):
        return "user_secret"
    return "agent"


def _require_metadata(source_type: str) -> dict[str, Any]:
    metadata = get_source_metadata(source_type)
    if metadata is None:
        raise SourceSetupError([f"Unknown source type '{source_type}'"])
    return metadata


def _resources_apply(metadata: dict[str, Any]) -> bool:
    """True when the wizard's _select_resources() would run (separate `resources` key)."""
    if not metadata.get("available_resources"):
        return False
    return not any(p.get("name") == "resources" for p in metadata.get("optional_params", []))


def _default_for(param: dict[str, Any], source_name: str | None) -> Any:
    default = param.get("default")
    if param["name"] == "directory" and default == "data/uploads" and source_name:
        return f"data/uploads/{source_name}"
    if default is None and param.get("type") == "date" and "default_days_ago" in param:
        return (date.today() - timedelta(days=param["default_days_ago"])).isoformat()
    return default


def get_setup_schema(source_type: str) -> dict[str, Any]:
    """Describe the parameters needed to set up a source type (raises for unknown types)."""
    metadata = _require_metadata(source_type)
    auth_type = metadata.get("auth_type")
    fields: list[dict[str, Any]] = []
    required_names = {p["name"] for p in metadata.get("required_params", [])}
    for param in metadata.get("required_params", []) + metadata.get("optional_params", []):
        managed = _managed_by(param, metadata, source_type)
        entry: dict[str, Any] = {
            "name": param["name"],
            "type": param.get("type", "string"),
            "required": param["name"] in required_names and managed != "oauth",
            "default": _default_for(param, None),
            "choices": param.get("choices"),
            "help": param.get("help", ""),
            "prompt": param.get("prompt", param["name"]),
            "managed_by": managed,
        }
        if managed == "user_secret":
            entry["env_var_template"] = param.get("env_var", param["name"].upper())
        fields.append(entry)
    resources = None
    if _resources_apply(metadata):
        available = list(metadata["available_resources"])
        resources = {
            "available": available,
            "default": list(metadata.get("default_resources", available)),
        }
    return {
        "source_type": source_type,
        "display_name": metadata.get("display_name", source_type),
        "description": metadata.get("description", ""),
        "auth_type": auth_type.value if isinstance(auth_type, AuthType) else str(auth_type),
        "setup_supported": bool(metadata.get("wizard_enabled", False))
        and source_type not in MCP_UNSUPPORTED_SOURCE_TYPES,
        "supports_empty_sync_policy": source_type in REPLACE_MODE_SOURCE_TYPES,
        "fields": fields,
        "resources": resources,
        "setup_guide": list(metadata.get("setup_guide", [])),
        "first_sync_note": metadata.get("first_sync_note"),
    }


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, (list, tuple)) and all(isinstance(i, str) for i in value):
        return list(value)
    raise ValueError("must be a list of strings or a comma-separated string")


def _coerce(param: dict[str, Any], value: Any) -> Any:
    """Coerce one supplied value; returns None for 'treat as absent'. Raises ValueError."""
    ptype = param.get("type", "string")
    if ptype in ("string", "text", "path"):
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.strip() or None
    if ptype in ("integer", "number"):
        if isinstance(value, bool):
            raise ValueError(f"must be a{'n integer' if ptype == 'integer' else ' number'}")
        try:
            if ptype == "integer":
                if isinstance(value, float):
                    raise ValueError
                return int(value)
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"must be a{'n integer' if ptype == 'integer' else ' number'}"
            ) from None
        return int(num) if num.is_integer() else num
    if ptype in ("boolean", "bool"):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _BOOL_STRINGS:
            return _BOOL_STRINGS[value.strip().lower()]
        raise ValueError("must be true or false")
    if ptype == "list":
        return _str_list(value) or None
    if ptype == "multiselect":
        items = _str_list(value)
        choices = param.get("choices")
        bad = [i for i in items if choices and i not in choices]
        if bad:
            raise ValueError(f"invalid choice(s) {bad}. Valid: {list(choices or [])}")
        return items
    if ptype == "choice":
        if value not in (param.get("choices") or []):
            raise ValueError(f"must be one of {param.get('choices')}")
        return value
    if ptype == "json":
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("is not valid JSON") from None
        raise ValueError("must be JSON (object, array, or JSON string)")
    if ptype == "date":
        if not isinstance(value, str):
            raise ValueError("must be a YYYY-MM-DD date string")
        text = value.strip()
        if not text:
            return None
        if _DAYS_AGO.match(text):
            return text
        try:
            date.fromisoformat(text)
        except ValueError:
            raise ValueError("must be YYYY-MM-DD (or a relative form like '90daysAgo')") from None
        return text
    if ptype == "sheet_selector":
        items = _str_list(value)
        if not items:
            raise ValueError("must list at least one sheet")
        return items
    return value


def normalize_params(
    source_type: str, source_name: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[CredentialRequirement]]:
    """Validate/coerce params against the registry. Pure (no I/O); raises with ALL errors."""
    metadata = _require_metadata(source_type)
    required = metadata.get("required_params", [])
    optional = metadata.get("optional_params", [])
    resources_ok = _resources_apply(metadata)
    known = {p["name"] for p in required + optional} | ({"resources"} if resources_ok else set())
    errors: list[str] = []
    out: dict[str, Any] = {}
    reqs: list[CredentialRequirement] = []

    for key in params:
        if key not in known:
            errors.append(f"Unknown parameter '{key}' for {source_type}. Valid: {sorted(known)}")

    required_names = {p["name"] for p in required}
    for param in required + optional:
        name = param["name"]
        managed = _managed_by(param, metadata, source_type)
        supplied = params.get(name)
        if managed in ("user_secret", "oauth") and _is_secret_param(param):
            env_var = compute_env_var_name(param, source_name)
            if supplied is not None and supplied != env_var:
                if managed == "oauth":
                    errors.append(f"'{name}' is managed by OAuth. Omit it")
                else:
                    errors.append(
                        f"'{name}' is a secret. Secret values are never accepted — omit it; "
                        f"Dango will ask the user to set {env_var} in .env"
                    )
                continue
            if managed == "user_secret":
                out[name] = env_var
                help_text = param.get("help") or param.get("prompt", "")
                reqs.append(
                    CredentialRequirement(
                        kind="env_var",
                        name=env_var,
                        detail=f"Set {env_var} in .env ({help_text})".strip(),
                    )
                )
            continue
        if managed == "oauth":
            if supplied is not None:
                errors.append(f"'{name}' is managed by OAuth. Omit it")
            continue
        value = None
        if supplied is not None:
            try:
                value = _coerce(param, supplied)
            except ValueError as exc:
                errors.append(f"Parameter '{name}' {exc}")
                continue
        if value is None:
            value = _default_for(param, source_name)
            if isinstance(value, list):
                value = list(value)
        if value is None:
            if name in required_names:
                errors.append(f"Missing required parameter '{name}'")
            continue
        out[name] = value

    if resources_ok and params.get("resources") is not None:
        available = metadata["available_resources"]
        try:
            items = _str_list(params["resources"])
            bad = [i for i in items if i not in available]
            if bad:
                errors.append(f"Unknown resource(s) {bad}. Available: {list(available)}")
            else:
                out["resources"] = items
        except ValueError as exc:
            errors.append(f"Parameter 'resources' {exc}")
    elif resources_ok:
        out["resources"] = list(metadata.get("default_resources", metadata["available_resources"]))

    if errors:
        raise SourceSetupError(errors)
    return out, reqs
