"""dango/ingestion/sources/setup_lifecycle.py

Non-interactive source update, enable/disable and removal with the same validation as create.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from dango.ingestion.sources.registry import AuthType, get_source_metadata
from dango.ingestion.sources.setup_schema import (
    MCP_UNSUPPORTED_SOURCE_TYPES,
    REPLACE_MODE_SOURCE_TYPES,
    CredentialRequirement,
    SourceSetupError,
    SourceSetupResult,
    _coerce,
    _managed_by,
    _resources_apply,
    compute_env_var_name,
    normalize_params,
)
from dango.ingestion.sources.setup_service import _dotenv_has, prepare_source_directory
from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "SourceRemovalResult",
    "credential_requirements",
    "remove_source",
    "set_source_enabled",
    "update_source",
]

_DATETIME_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")


@dataclass
class SourceRemovalResult:
    """What remove_source removed (or, for dry_run, would remove)."""

    source_name: str
    source_type: str
    status: str  # "removed" | "dry_run"
    files_removed: list[str] = field(default_factory=list)  # project-relative
    config_toml_section_removed: bool = False
    downstream_models: list[str] = field(default_factory=list)  # "layer.name"
    env_vars_matching: list[str] = field(default_factory=list)  # names only, never values
    monitors_removed: list[str] = field(default_factory=list)  # analysis monitor names
    warnings: list[str] = field(default_factory=list)


def _pydantic_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]


def _load_with_source(project_root: Path, source_name: str) -> tuple[Any, Any]:
    from dango.config.helpers import load_config

    config = load_config(project_root)
    source = config.sources.get_source(source_name)
    if source is None:
        names = ", ".join(s.name for s in config.sources.sources) or "none"
        raise SourceSetupError([f"Source '{source_name}' not found. Available sources: {names}"])
    return config, source


def _oauth_requirements(
    project_root: Path, source_type: str, metadata: dict[str, Any]
) -> list[CredentialRequirement]:
    """Same OAuth check as prepare_source (never constructs OAuthStorage without secrets.toml)."""
    if metadata.get("auth_type") != AuthType.OAUTH:
        return []
    from dango.oauth.router import OAUTH_PROVIDER_MAP

    if source_type not in OAUTH_PROVIDER_MAP:
        return [
            CredentialRequirement(
                kind="secrets_toml",
                detail="Configure credentials manually in .dlt/secrets.toml",
            )
        ]
    from dango.oauth.storage import OAuthStorage

    has_secrets = (project_root / ".dlt" / "secrets.toml").exists()
    cred = OAuthStorage(project_root).get(source_type) if has_secrets else None
    if cred is None or cred.is_expired():
        state = "missing" if cred is None else "expired"
        return [
            CredentialRequirement(
                kind="oauth",
                command=f"dango oauth {source_type}",
                detail=f"OAuth credentials are {state}; run 'dango oauth {source_type}'",
            )
        ]
    return []


def _block_values(source: Any) -> dict[str, Any]:
    st: str = source.type.value
    from dango.config.models import DataSource

    block = getattr(source, st) if st in DataSource.model_fields else source.generic_config
    if isinstance(block, BaseModel):
        return block.model_dump(mode="json", exclude_none=True)
    return dict(block or {})


def credential_requirements(project_root: Path, source: Any) -> list[CredentialRequirement]:
    """Credentials a configured source still needs (env vars unset in env/.env, OAuth).

    Read-only and uncached; never reports values and never builds OAuthStorage without
    .dlt/secrets.toml.
    """
    st: str = source.type.value
    metadata = get_source_metadata(st)
    if metadata is None:
        return []
    existing = _block_values(source)
    reqs: list[CredentialRequirement] = []
    required_names = {p["name"] for p in metadata.get("required_params", [])}
    for param in metadata.get("required_params", []) + metadata.get("optional_params", []):
        name = param["name"]
        if _managed_by(param, metadata, st) != "user_secret":
            continue
        if name not in existing and name not in required_names:
            continue
        env_var = str(existing.get(name) or compute_env_var_name(param, source.name))
        if not _dotenv_has(project_root, env_var):
            reqs.append(
                CredentialRequirement(kind="env_var", name=env_var, detail=f"Set {env_var} in .env")
            )
    reqs.extend(_oauth_requirements(project_root, st, metadata))
    return reqs


def update_source(
    project_root: Path,
    source_name: str,
    params: dict[str, Any] | None = None,
    *,
    description: str | None = None,
    empty_sync_policy: str | None = None,
) -> SourceSetupResult:
    """Update a source's settings with the same validation as create_source.

    Existing values are never rewritten unless the caller supplies that key. Supplying a key as
    None/empty resets it to the registry default (create semantics), or removes it if none.
    """
    from dango.config import ConfigLoader
    from dango.config.models import DataSource

    if not params and description is None and empty_sync_policy is None:
        raise SourceSetupError(["Nothing to update"])
    params = dict(params or {})
    config, source = _load_with_source(project_root, source_name)
    st: str = source.type.value
    metadata = get_source_metadata(st)
    if st in MCP_UNSUPPORTED_SOURCE_TYPES:
        raise SourceSetupError([f"'{st}' sources must be edited via 'dango source edit'"])
    if metadata is None:
        raise SourceSetupError([f"Unknown source type '{st}'"])

    block = getattr(source, st) if st in DataSource.model_fields else source.generic_config
    if isinstance(block, BaseModel):
        existing: dict[str, Any] = block.model_dump(mode="json", exclude_none=True)
    else:
        existing = dict(block or {})

    # Validation input: existing registry keys (secret/oauth excluded, datetimes as dates) + params
    reg_params = {
        p["name"]: p
        for p in metadata.get("required_params", []) + metadata.get("optional_params", [])
    }
    candidate: dict[str, Any] = {}
    for key, value in existing.items():
        param = reg_params.get(key)
        if param is None:
            if key == "resources" and _resources_apply(metadata):
                candidate[key] = value
            continue
        if _managed_by(param, metadata, st) != "agent":
            continue
        if param.get("type") == "date" and isinstance(value, str):
            m = _DATETIME_PREFIX_RE.match(value)
            value = m.group(1) if m else value
        if key not in params:
            try:
                _coerce(param, value)
            except ValueError:
                continue  # stale stored value; left untouched, not re-validated
        candidate[key] = value
    candidate.update(params)

    errors: list[str] = []
    normalized: dict[str, Any] = {}
    if params:
        try:
            normalized, _ = normalize_params(st, source_name, candidate)
        except SourceSetupError as exc:
            errors.extend(exc.errors)

    final = dict(existing)
    for key in params:
        if key in normalized:
            final[key] = normalized[key]
        else:
            final.pop(key, None)

    new = source.model_dump(mode="json", exclude_none=True)
    if description is not None:
        new["description"] = description
    if empty_sync_policy is not None:
        if st not in REPLACE_MODE_SOURCE_TYPES:
            errors.append("empty_sync_policy only applies to replace-mode sources")
        elif empty_sync_policy not in ("block", "allow"):
            errors.append("empty_sync_policy must be 'block' or 'allow'")
        else:
            new["empty_sync_policy"] = empty_sync_policy

    directory_changed = (
        st in ("csv", "local_files") and "directory" in params and "directory" in final
    )
    if directory_changed:
        root = project_root.resolve()
        if not (root / str(final["directory"])).resolve().is_relative_to(root):
            errors.append(f"directory '{final['directory']}' must be inside the project")

    new["generic_config" if st not in DataSource.model_fields else st] = final
    if not errors:
        try:
            DataSource(**new)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc))
    if errors:
        raise SourceSetupError(errors)

    result = SourceSetupResult(source_name=source_name, source_type=st, source_config=new)
    if directory_changed:
        fixed, dir_warnings, created = prepare_source_directory(project_root, source_name, final)
        final["directory"] = fixed["directory"]
        result.warnings.extend(dir_warnings)
        result.files_changed.extend(created)

    config.sources.sources[config.sources.sources.index(source)] = DataSource(**new)
    ConfigLoader(project_root).save_sources_config(config.sources)
    result.files_changed.append(".dango/sources.yml")

    result.credentials_required = credential_requirements(project_root, DataSource(**new))
    _, result.validation_errors = ConfigLoader(project_root).validate_config()
    return result


def set_source_enabled(project_root: Path, source_name: str, enabled: bool) -> bool:
    """Enable/disable a source. True if changed, False if it was already in that state."""
    from dango.config import ConfigLoader

    config, source = _load_with_source(project_root, source_name)
    if source.enabled == enabled:
        return False
    source.enabled = enabled
    ConfigLoader(project_root).save_sources_config(config.sources)
    return True


def _owned_staging(stem: str, name: str, other_names: list[str]) -> bool:
    """True if staging model/table `stem` belongs to source `name` (not a `name__x` source)."""
    if not stem.startswith(f"stg_{name}__"):
        return False
    return not any(stem.startswith(f"stg_{o}__") for o in other_names if o.startswith(f"{name}__"))


def _env_vars_matching(project_root: Path, name: str, other_names: list[str]) -> list[str]:
    """Names (never values) of .env vars for this source, minus longer-named sources' vars."""
    env_file = project_root / ".env"
    if not env_file.exists():
        return []
    from dango.utils.env_file import parse_env_file

    def token(n: str) -> str:
        """Env var prefix for a source name."""
        return n.upper().replace("-", "_")

    def belongs(var: str, n: str) -> bool:
        """True if var is named for source n."""
        return var.startswith(token(n) + "_") or var == token(n)

    longer = [o for o in other_names if o.startswith(f"{name}_")]
    return [
        k
        for k in parse_env_file(env_file.read_text())
        if belongs(k, name) and not any(belongs(k, o) for o in longer)
    ]


def _downstream_models(project_root: Path, name: str, other_names: list[str]) -> list[str]:
    from dango.transformation.model_common import CUSTOM_MODEL_LAYERS, iter_model_files
    from dango.transformation.model_sql import extract_refs

    found: list[str] = []
    for path, layer in iter_model_files(project_root):
        if layer not in CUSTOM_MODEL_LAYERS:
            continue
        try:
            refs, sources = extract_refs(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue  # unreadable model file: cannot be shown to depend on the source
        if any(_owned_staging(r, name, other_names) for r in refs) or any(
            s.split(".")[0] == name for s in sources
        ):
            found.append(f"{layer}.{path.stem}")
    return found


def _source_monitors(project_root: Path, name: str, other_names: list[str]) -> list[str]:
    """Monitors on raw_{name}.<t> or staging.stg_{name}__<x> (how add_analysis_monitors writes)."""
    from dango.analysis.config import load_monitors_config

    out: list[str] = []
    for m in load_monitors_config(project_root).monitors:
        schema, _, table = m.source_table.partition(".")
        if schema == f"raw_{name}" or (
            schema == "staging" and _owned_staging(table, name, other_names)
        ):
            out.append(m.name)
    return out


def _remove_config_toml_section(
    project_root: Path, st: str, name: str, same_type: list[str], result: SourceRemovalResult
) -> None:
    config_toml = project_root / ".dlt" / "config.toml"
    if not config_toml.exists():
        return
    try:
        import tomlkit

        doc = tomlkit.parse(config_toml.read_text())
        sources_section: Any = doc.get("sources", {})
        if st not in sources_section:
            return
        if same_type:
            result.warnings.append(
                f"Kept [sources.{st}] in .dlt/config.toml — still used by: {', '.join(same_type)}"
            )
            return
        result.config_toml_section_removed = True
        if result.status == "dry_run":
            return
        del sources_section[st]
        if not sources_section:
            del doc["sources"]
        config_toml.write_text(tomlkit.dumps(doc))
    except Exception as exc:
        result.config_toml_section_removed = False
        result.warnings.append(f"Could not clean up .dlt/config.toml: {exc}")


def remove_source(
    project_root: Path,
    source_name: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> SourceRemovalResult:
    """Remove a source: sources.yml entry, staging files, config.toml section, monitors.

    Never drops warehouse data and never edits .env (matching names are only reported).
    """
    from dango.config import ConfigLoader

    config, source = _load_with_source(project_root, source_name)
    st: str = source.type.value
    others = [s for s in config.sources.sources if s.name != source_name]
    other_names = [s.name for s in others]
    result = SourceRemovalResult(
        source_name=source_name, source_type=st, status="dry_run" if dry_run else "removed"
    )

    result.downstream_models = _downstream_models(project_root, source_name, other_names)
    if result.downstream_models and not force and not dry_run:
        raise SourceSetupError(
            [
                "Models depend on this source's staging models: "
                f"{', '.join(result.downstream_models)}. "
                "Update or remove them first, or pass force=True."
            ]
        )

    staging_dir = project_root / "dbt" / "models" / "staging"
    staging_files: list[Path] = []
    if staging_dir.exists():
        staging_files = [
            p
            for p in sorted(staging_dir.glob(f"stg_{source_name}__*.sql"))
            if _owned_staging(p.stem, source_name, other_names)
        ]
        staging_files += [
            p
            for p in (
                staging_dir / f"sources_{source_name}.yml",
                staging_dir / f"stg_{source_name}.yml",
            )
            if p.exists()
        ]
    result.files_removed = [str(p.relative_to(project_root)) for p in staging_files]
    result.env_vars_matching = _env_vars_matching(project_root, source_name, other_names)
    try:
        result.monitors_removed = _source_monitors(project_root, source_name, other_names)
    except Exception as exc:
        result.warnings.append(f"Could not read monitors config: {exc}")

    if not dry_run:
        config.sources.sources = others
        ConfigLoader(project_root).save_sources_config(config.sources)
    _remove_config_toml_section(
        project_root, st, source_name, [s.name for s in others if s.type.value == st], result
    )
    if not dry_run:
        for path in staging_files:
            path.unlink()
        if result.monitors_removed:
            try:
                from dango.analysis.config import load_monitors_config, save_monitors_config
                from dango.analysis.models import MonitorsConfig

                cfg = load_monitors_config(project_root)
                kept = [m for m in cfg.monitors if m.name not in result.monitors_removed]
                save_monitors_config(
                    project_root, MonitorsConfig(enabled=cfg.enabled, monitors=kept)
                )
            except Exception as exc:
                result.monitors_removed = []
                result.warnings.append(f"Could not update monitors.yml: {exc}")
    result.warnings.append("Warehouse data is not deleted — run `dango db clean` to remove it")
    return result
