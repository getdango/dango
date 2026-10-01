"""dango/ingestion/sources/setup_service.py

Non-interactive source setup: schema, parameter validation, and persistence shared by the wizard and MCP.
"""

import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dango.ingestion.sources.registry import AuthType, get_source_metadata
from dango.ingestion.sources.setup_schema import (
    MCP_UNSUPPORTED_SOURCE_TYPES,
    REPLACE_MODE_SOURCE_TYPES,
    CredentialRequirement,
    SourceSetupError,
    SourceSetupResult,
    _is_secret_param,
    _require_metadata,
    compute_env_var_name,
    get_setup_schema,
    is_credential_param,
    normalize_params,
)
from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "MCP_UNSUPPORTED_SOURCE_TYPES",
    "REPLACE_MODE_SOURCE_TYPES",
    "CredentialRequirement",
    "PreparedSource",
    "SourceSetupError",
    "SourceSetupResult",
    "add_analysis_monitors",
    "append_source",
    "apply_source",
    "build_source_config",
    "compute_env_var_name",
    "create_source",
    "get_setup_schema",
    "is_credential_param",
    "normalize_params",
    "prepare_source",
    "prepare_source_directory",
    "provision_geo_targets",
    "write_default_config",
    "write_secrets_toml_template",
]


@dataclass
class PreparedSource:
    """A fully validated source ready for apply_source (no writes performed yet)."""

    source_type: str
    source_name: str
    params: dict[str, Any]
    source_config: dict[str, Any]
    credentials_required: list[CredentialRequirement]
    metadata: dict[str, Any]


def build_source_config(
    source_type: str,
    source_name: str,
    params: dict[str, Any],
    *,
    description: str | None = None,
    empty_sync_policy: str | None = None,
) -> dict[str, Any]:
    """Build the sources.yml entry (type block, registry lookback, empty-sync policy)."""
    from dango.config.models import DataSource

    metadata = _require_metadata(source_type)
    config: dict[str, Any] = {
        "name": source_name,
        "type": source_type,
        "enabled": True,
        "description": description or f"{metadata.get('display_name')} - added via wizard",
    }
    if source_type in DataSource.model_fields:
        config[source_type] = params or {}
    else:
        config["generic_config"] = params or {}

    default_lookback = (metadata.get("default_config") or {}).get("lookback_days")
    if default_lookback is not None:
        config["lookback_days"] = default_lookback

    if source_type in REPLACE_MODE_SOURCE_TYPES:
        policy = empty_sync_policy or "block"
        if policy not in ("block", "allow"):
            raise SourceSetupError(["empty_sync_policy must be 'block' or 'allow'"])
        config["empty_sync_policy"] = policy
    elif empty_sync_policy is not None:
        raise SourceSetupError(["empty_sync_policy only applies to replace-mode sources"])
    return config


def write_default_config(
    project_root: Path,
    source_type: str,
    *,
    confirm_overwrite: Callable[[str], bool] | None = None,
) -> bool:
    """Write the registry default_config to .dlt/config.toml. True if the file was written."""
    try:
        import tomlkit

        default_config = (get_source_metadata(source_type) or {}).get("default_config", {})
        if not default_config:
            return False
        dlt_dir = project_root / ".dlt"
        config_path = dlt_dir / "config.toml"
        dlt_dir.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            doc = tomlkit.parse(config_path.read_text())
        else:
            doc = tomlkit.document()
        if "sources" not in doc:
            doc.add("sources", tomlkit.table())
        if source_type not in doc["sources"]:  # type: ignore[operator]
            doc["sources"].add(source_type, tomlkit.table())  # type: ignore[union-attr]
        source_table = doc["sources"][source_type]  # type: ignore[index]

        for key, value in default_config.items():
            if key in source_table:
                if confirm_overwrite is None or not confirm_overwrite(key):
                    continue  # keep existing value
                source_table[key] = value  # in-place update preserves key position
                continue
            if key == "queries":
                for note in (
                    "",
                    "Default queries for Google Analytics 4",
                    "Each query creates a table with the specified dimensions and metrics",
                    "Customize by editing, adding, or removing queries",
                    "GA4 API limits: max 9 dimensions, 10 metrics per query",
                    "Docs: https://developers.google.com/analytics/devguides/reporting/data/v1",
                    "",
                ):
                    source_table.add(tomlkit.comment(note))
            source_table.add(key, value)

        config_path.write_text(tomlkit.dumps(doc))
        return True
    except Exception as exc:
        logger.warning("default_config_write_failed", source_type=source_type, error=str(exc))
        return False


def provision_geo_targets(project_root: Path, source_name: str) -> list[str]:
    """Provision the geo_targets seed + staging join model (Google Ads). Returns new paths."""
    templates_dir = Path(__file__).resolve().parents[2] / "templates" / "dbt"
    created: list[str] = []
    seeds_dir = project_root / "dbt" / "seeds"
    seed_dest = seeds_dir / "geo_targets.csv"
    if not seed_dest.exists():
        seeds_dir.mkdir(parents=True, exist_ok=True)
        seed_src = templates_dir / "seeds" / "geo_targets.csv"
        seed_dest.write_text(seed_src.read_text(encoding="utf-8"), encoding="utf-8")
        created.append("dbt/seeds/geo_targets.csv")
    staging_dir = project_root / "dbt" / "models" / "staging"
    model_dest = staging_dir / f"stg_{source_name}__geo_names.sql"
    if not model_dest.exists():
        staging_dir.mkdir(parents=True, exist_ok=True)
        template = (templates_dir / "stg_geo_names.sql").read_text(encoding="utf-8")
        model_dest.write_text(template.replace("__SOURCE_NAME__", source_name), encoding="utf-8")
        created.append(f"dbt/models/staging/stg_{source_name}__geo_names.sql")
    return created


def prepare_source_directory(
    project_root: Path, source_name: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """File-source directory handling. Returns (params, warnings, created_paths)."""
    params = dict(params)
    warnings: list[str] = []
    created: list[str] = []
    raw_directory = params["directory"]
    raw_path = Path(raw_directory)

    if raw_path.is_absolute():
        try:
            rel_path: Path | None = raw_path.relative_to(project_root)
        except ValueError:
            rel_path = None
        if rel_path is not None:
            warnings.append(
                f"'{raw_directory}' is an absolute path. "
                f"Using relative path '{rel_path}' instead — portable across machines/cloud."
            )
            params["directory"] = str(rel_path)
        else:
            warnings.append(
                f"'{raw_directory}' is an absolute path outside the project. "
                "Consider a path relative to the project root (e.g. data/uploads/...) "
                "so it works on other machines and in the cloud."
            )

    directory_path = project_root / params["directory"]
    if not directory_path.exists():
        directory_path.mkdir(parents=True, exist_ok=True)
        created.append(params["directory"])
    default_dir = f"data/uploads/{source_name}"
    if params["directory"] != default_dir and not params["directory"].startswith("data/uploads"):
        warnings.append(f"Remember to add '{params['directory']}' to .gitignore")
    return params, warnings, created


def write_secrets_toml_template(
    project_root: Path, source_type: str, source_name: str, params: dict[str, Any]
) -> str | None:
    """Append the registry secrets.toml template if absent. Returns the path written, or None."""
    secrets_template = (get_source_metadata(source_type) or {}).get("secrets_toml_template")
    if not secrets_template:
        return None
    dlt_dir = project_root / ".dlt"
    secrets_path = dlt_dir / "secrets.toml"
    dlt_dir.mkdir(parents=True, exist_ok=True)
    existing = secrets_path.read_text() if secrets_path.exists() else ""
    if f"[sources.{source_name}." in existing:
        return None
    template_vars = defaultdict(str, source_name=source_name, **params)
    template_text = secrets_template.format_map(template_vars)
    prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
    secrets_path.write_text(prefix + template_text + "\n")
    return ".dlt/secrets.toml"


def append_source(project_root: Path, source_config: dict[str, Any]) -> None:
    """Append a DataSource to sources.yml (rejects duplicates and invalid configs)."""
    from dango.config.helpers import load_config, save_config
    from dango.config.models import DataSource

    config = load_config(project_root)
    if config.sources.get_source(source_config["name"]) is not None:
        raise SourceSetupError([f"Source '{source_config['name']}' already exists"])
    try:
        source = DataSource(**source_config)
    except ValidationError as exc:
        raise SourceSetupError(
            [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        ) from exc
    config.sources.sources.append(source)
    save_config(config, project_root)


def _dotenv_has(project_root: Path, name: str) -> bool:
    from dotenv import dotenv_values

    if os.environ.get(name):
        return True
    env_file = project_root / ".env"
    return env_file.exists() and bool(dotenv_values(env_file).get(name))


def prepare_source(
    project_root: Path,
    source_type: str,
    source_name: str,
    params: dict[str, Any],
    *,
    description: str | None = None,
    empty_sync_policy: str | None = None,
    validate_params: bool = True,
) -> PreparedSource:
    """Validate everything and build the config. Performs no writes."""
    from dango.config.helpers import load_config
    from dango.config.models import DataSource

    errors: list[str] = []
    metadata = get_source_metadata(source_type)
    if metadata is None:
        raise SourceSetupError([f"Unknown source type '{source_type}'"])

    if source_name != source_name.lower():
        errors.append(f"Source name '{source_name}' must be lowercase")
    try:
        DataSource.validate_name_format(source_name)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        if load_config(project_root).sources.get_source(source_name) is not None:
            errors.append(f"Source '{source_name}' already exists")
    except Exception as exc:
        errors.append(f"Could not load project config: {exc}")

    norm_params = dict(params)
    reqs: list[CredentialRequirement] = []
    if validate_params:
        try:
            norm_params, reqs = normalize_params(source_type, source_name, params)
        except SourceSetupError as exc:
            errors.extend(exc.errors)
    reqs = [
        r
        for r in reqs
        if not (r.kind == "env_var" and r.name and _dotenv_has(project_root, r.name))
    ]

    if metadata.get("auth_type") == AuthType.OAUTH:
        from dango.oauth.router import OAUTH_PROVIDER_MAP

        if source_type not in OAUTH_PROVIDER_MAP:
            reqs.append(
                CredentialRequirement(
                    kind="secrets_toml",
                    detail="Configure credentials manually in .dlt/secrets.toml",
                )
            )
        else:
            from dango.oauth.storage import OAuthStorage

            # OAuthStorage() writes secrets.toml on construction; prepare must not write
            has_secrets = (project_root / ".dlt" / "secrets.toml").exists()
            cred = OAuthStorage(project_root).get(source_type) if has_secrets else None
            if cred is None or cred.is_expired():
                state = "missing" if cred is None else "expired"
                reqs.append(
                    CredentialRequirement(
                        kind="oauth",
                        command=f"dango oauth {source_type}",
                        detail=f"OAuth credentials are {state}; run 'dango oauth {source_type}'",
                    )
                )

    directory = norm_params.get("directory")
    if source_type in ("csv", "local_files") and isinstance(directory, str):
        root = project_root.resolve()
        if not (root / directory).resolve().is_relative_to(root):
            errors.append(f"directory '{directory}' must be inside the project")

    source_config: dict[str, Any] = {}
    try:
        source_config = build_source_config(
            source_type,
            source_name,
            norm_params,
            description=description,
            empty_sync_policy=empty_sync_policy,
        )
    except SourceSetupError as exc:
        errors.extend(exc.errors)
    if source_config and not errors:
        try:
            DataSource(**source_config)
        except ValidationError as exc:
            errors.extend(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    if errors:
        raise SourceSetupError(errors)
    return PreparedSource(source_type, source_name, norm_params, source_config, reqs, metadata)


def _env_entries(prepared: PreparedSource, names: set[str]) -> list[dict[str, str]]:
    meta = prepared.metadata
    entries = []
    for param in meta.get("required_params", []) + meta.get("optional_params", []):
        if not _is_secret_param(param):
            continue
        env_var = compute_env_var_name(param, prepared.source_name)
        if env_var not in names:
            continue
        entries.append(
            {
                "name": env_var,
                "param_name": param["name"],
                "display_name": param.get("prompt", env_var),
                "help": param.get("help", ""),
                "format": param.get("format", ""),
                "example": param.get("example", ""),
                "source_name": prepared.source_name,
                "source_type": meta.get("display_name", prepared.source_type),
            }
        )
    return entries


def apply_source(
    project_root: Path,
    prepared: PreparedSource,
    *,
    write_env_template: bool = True,
    confirm_overwrite: Callable[[str], bool] | None = None,
) -> SourceSetupResult:
    """Perform the writes for a prepared source."""
    from dango.config import ConfigLoader

    st, name = prepared.source_type, prepared.source_name
    meta = prepared.metadata
    config = prepared.source_config
    result = SourceSetupResult(
        source_name=name,
        source_type=st,
        source_config=config,
        credentials_required=list(prepared.credentials_required),
    )

    if meta.get("default_config") and write_default_config(
        project_root, st, confirm_overwrite=confirm_overwrite
    ):
        result.files_changed.append(".dlt/config.toml")
    if st == "google_ads":
        result.files_changed.extend(provision_geo_targets(project_root, name))
    if st in ("csv", "local_files") and "directory" in prepared.params:
        new_params, dir_warnings, created = prepare_source_directory(
            project_root, name, prepared.params
        )
        config[st] = new_params
        result.warnings.extend(dir_warnings)
        result.files_changed.extend(created)

    env_reqs = {r.name for r in result.credentials_required if r.kind == "env_var" and r.name}
    if env_reqs and write_env_template:
        from dango.cli.env_helpers import create_env_template

        create_env_template(project_root / ".env", _env_entries(prepared, env_reqs))  # type: ignore[arg-type]
        result.files_changed.append(".env")

    append_source(project_root, config)
    result.files_changed.append(".dango/sources.yml")

    if not env_reqs and meta.get("auth_type") != AuthType.OAUTH:
        try:
            written = write_secrets_toml_template(project_root, st, name, prepared.params)
        except Exception as exc:
            result.warnings.append(f"Could not write secrets template: {exc}")
            written = None
        if written:
            result.files_changed.append(written)
            result.credentials_required.append(
                CredentialRequirement(
                    kind="secrets_toml",
                    detail="Fill in the credential values in .dlt/secrets.toml before syncing",
                )
            )

    _, result.validation_errors = ConfigLoader(project_root).validate_config()
    return result


def create_source(
    project_root: Path,
    source_type: str,
    source_name: str,
    params: dict[str, Any],
    *,
    description: str | None = None,
    empty_sync_policy: str | None = None,
) -> SourceSetupResult:
    """Validate and create a source end to end (the MCP entry point)."""
    metadata = get_source_metadata(source_type)
    if (
        metadata is None
        or source_type in MCP_UNSUPPORTED_SOURCE_TYPES
        or not metadata.get("wizard_enabled", False)
    ):
        raise SourceSetupError([f"Source type '{source_type}' cannot be set up non-interactively"])
    return apply_source(
        project_root,
        prepare_source(
            project_root,
            source_type,
            source_name,
            params,
            description=description,
            empty_sync_policy=empty_sync_policy,
        ),
    )


def add_analysis_monitors(project_root: Path, source_type: str, source_name: str) -> int:
    """Add the pre-built analysis monitors for a source. Returns the number added (never raises)."""
    try:
        from dango.analysis.config import add_monitors_to_config
        from dango.analysis.templates import generate_metrics_for_source

        templates = generate_metrics_for_source(source_type, source_name)
        if not templates:
            return 0
        header = None
        if source_type in ("csv", "local_files"):
            header = (
                f"NOTE: Tables will be created in the raw_{source_name}"
                f" schema. Replace 'your_table' with your actual"
                f" table name after first sync."
            )
        add_monitors_to_config(project_root, templates, header_comment=header)
        return len(templates)
    except Exception:
        return 0
