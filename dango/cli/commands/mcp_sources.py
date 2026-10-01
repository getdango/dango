"""dango/cli/commands/mcp_sources.py

MCP source tools: discover types, configure, update, enable/disable, validate and remove data sources.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import shutil
from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root, _git_warnings
from dango.cli.commands.mcp_server import mcp

_FILE_SUFFIXES = {".csv", ".json", ".jsonl", ".ndjson", ".parquet"}


def _service_error(e: Any) -> dict[str, Any]:
    """Service errors verbatim: agents fix settings from these exact messages."""
    return {"error": "; ".join(e.errors), "errors": list(e.errors)}


def _creds(reqs: Any) -> list[dict[str, Any]]:
    return [dataclasses.asdict(r) for r in reqs]


def _next_steps(source_name: str, reqs: Any) -> list[str]:
    steps: list[str] = []
    for r in reqs:
        if r.kind == "env_var":
            steps.append(f"Ask the user to set {r.name} in .env")
        elif r.kind == "oauth":
            steps.append(f"Ask the user to run: {r.command}")
        else:
            steps.append(f"Ask the user to fill credentials in .dlt/secrets.toml ({r.detail})")
    steps.append(
        f"Then call validate_source('{source_name}') and run_sync(source_name='{source_name}')"
    )
    return steps


def _with_git_warning(result: dict[str, Any], project_root: Path) -> dict[str, Any]:
    try:
        warnings = _git_warnings(project_root)
    except Exception:  # noqa: BLE001 - the change already landed; never report it as failed
        warnings = []
    if warnings:
        result["git_warning"] = warnings
    return result


def _result_dict(r: Any, status: str, extra_files: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "source_name": r.source_name,
        "source_type": r.source_type,
        "source_config": r.source_config,
        "files_changed": list(r.files_changed) + (extra_files or []),
        "credentials_required": _creds(r.credentials_required),
        "warnings": list(r.warnings),
        "validation_errors": list(r.validation_errors),
        "next_steps": _next_steps(r.source_name, r.credentials_required),
    }


@mcp.tool()
def list_source_types() -> list[dict[str, Any]]:
    """List all available source types in the Dango registry.

    Returns list of dicts with: type, name, description, auth_type, category, setup_supported
    (False means the type can only be configured with the `dango source add` CLI wizard).
    """
    from dango.ingestion.sources.registry import SOURCE_REGISTRY
    from dango.ingestion.sources.setup_service import get_setup_schema

    out = []
    for k, v in SOURCE_REGISTRY.items():
        if not v.get("wizard_enabled", True):
            continue
        try:
            supported = bool(get_setup_schema(k)["setup_supported"])
        except Exception:  # noqa: BLE001
            supported = False
        out.append(
            {
                "type": k,
                "name": v.get("display_name", k),
                "description": v.get("description", ""),
                "auth_type": v.get("auth_type", "none"),
                "category": v.get("category", "other"),
                "setup_supported": supported,
            }
        )
    return out


@mcp.tool()
def get_source_setup_schema(source_type: str) -> dict[str, Any]:
    """Describe the settings a source type needs (fields, defaults, credential handling).

    Supply only fields with `managed_by == "agent"` to create_source/update_source. Fields marked
    `user_secret` or `oauth` are handled by the user (env vars, `dango oauth <type>`); never ask
    for or pass secret values. `rest_api`, `dlt_native` and types with `setup_supported: false`
    are CLI-only.
    """
    try:
        from dango.ingestion.sources.setup_service import SourceSetupError, get_setup_schema

        try:
            return dict(get_setup_schema(source_type))
        except SourceSetupError as e:
            return _service_error(e)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not read setup schema: {type(e).__name__}"}


def _validate_file(src: Path, prepared: Any, project_root: Path) -> tuple[Path | None, str | None]:
    """Return (destination, error) for a local file to be copied into the source directory."""
    if not src.is_file():
        return None, f"file_path '{src}' does not exist or is not a regular file"
    if src.suffix.lower() not in _FILE_SUFFIXES:
        return None, f"Unsupported file type '{src.suffix}'; use one of {sorted(_FILE_SUFFIXES)}"
    pattern = str(prepared.params.get("file_pattern") or "*")
    if not fnmatch.fnmatch(src.name, pattern):
        return None, f"File name '{src.name}' does not match file_pattern '{pattern}'"
    dest = project_root / str(prepared.params["directory"]) / src.name
    if dest.exists() and dest.resolve() != src.resolve():
        if dest.read_bytes() != src.read_bytes():
            return None, f"A different file already exists at {dest.relative_to(project_root)}"
    return dest, None


@mcp.tool()
def create_source(
    source_type: str,
    source_name: str,
    config: dict[str, Any] | None = None,
    description: str | None = None,
    empty_sync_policy: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Configure a new data source with validated settings.

    Call get_source_setup_schema(source_type) first and pass only agent-managed fields in
    `config`. Never pass secret values: credentials come back in `credentials_required` with
    `next_steps` to relay to the user (set an env var in .env, run `dango oauth <type>`).
    For a local CSV/JSON/Parquet file give `file_path` (local_files only): it is copied into the
    project (data/uploads/<source_name>/) so it works on other machines and in the cloud.
    Then validate_source and run_sync.

    Returns dict with: status, source_name, source_type, source_config, files_changed,
    credentials_required, warnings, validation_errors, next_steps, git_warning (if any),
    or error/errors.
    """
    project_root = _get_project_root()
    if file_path and source_type != "local_files":
        return {
            "error": "file_path is only supported for local_files sources",
            "errors": ["file_path is only supported for local_files sources"],
        }
    copied: Path | None = None
    made_dir: Path | None = None
    try:
        from dango.ingestion.sources.setup_service import (
            SourceSetupError,
            apply_source,
            prepare_source,
        )

        try:
            params = dict(config or {})
            if file_path:
                params.setdefault("directory", f"data/uploads/{source_name}")
            prepared = prepare_source(
                project_root,
                source_type,
                source_name,
                params,
                description=description,
                empty_sync_policy=empty_sync_policy,
            )
            extra: list[str] = []
            if file_path:
                src = Path(file_path)
                src = src if src.is_absolute() else project_root / src
                dest, err = _validate_file(src, prepared, project_root)
                if err or dest is None:
                    return {"error": err, "errors": [err]}
                if not dest.exists():
                    if not dest.parent.exists():
                        made_dir = dest.parent
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    copied = dest
                extra.append(str(dest.relative_to(project_root)))
            result = apply_source(project_root, prepared)
        except SourceSetupError as e:
            _undo_copy(copied, made_dir)
            return _service_error(e)
        except Exception:
            _undo_copy(copied, made_dir)
            raise
    except Exception as e:  # noqa: BLE001
        return {"error": f"create_source failed: {type(e).__name__}: {e}"}
    return _with_git_warning(_result_dict(result, "created", extra), project_root)


def _undo_copy(copied: Path | None, made_dir: Path | None) -> None:
    try:
        if copied is not None:
            copied.unlink(missing_ok=True)
        if made_dir is not None and made_dir.exists() and not any(made_dir.iterdir()):
            made_dir.rmdir()
    except OSError:
        pass


@mcp.tool()
def update_source(
    source_name: str,
    config: dict[str, Any] | None = None,
    description: str | None = None,
    empty_sync_policy: str | None = None,
) -> dict[str, Any]:
    """Update a source's settings with the same validation as create_source.

    Pass only the keys to change in `config`; a key given as None/empty resets to its default.
    Never pass secret values.

    Returns the same shape as create_source with status "updated", or error/errors.
    """
    project_root = _get_project_root()
    try:
        from dango.ingestion.sources.setup_lifecycle import update_source as _update
        from dango.ingestion.sources.setup_schema import SourceSetupError

        try:
            r = _update(
                project_root,
                source_name,
                config,
                description=description,
                empty_sync_policy=empty_sync_policy,
            )
        except SourceSetupError as e:
            return _service_error(e)
    except Exception as e:  # noqa: BLE001
        return {"error": f"update_source failed: {type(e).__name__}: {e}"}
    return _with_git_warning(_result_dict(r, "updated"), project_root)


@mcp.tool()
def set_source_enabled(source_name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a source (disabled sources are skipped by sync).

    Returns dict with: status ("enabled", "disabled" or "unchanged"), source_name, git_warning.
    """
    project_root = _get_project_root()
    try:
        from dango.ingestion.sources.setup_lifecycle import set_source_enabled as _set
        from dango.ingestion.sources.setup_schema import SourceSetupError

        try:
            changed = _set(project_root, source_name, enabled)
        except SourceSetupError as e:
            return _service_error(e)
    except Exception as e:  # noqa: BLE001
        return {"error": f"set_source_enabled failed: {type(e).__name__}: {e}"}
    result = {
        "status": ("enabled" if enabled else "disabled") if changed else "unchanged",
        "source_name": source_name,
    }
    return _with_git_warning(result, project_root) if changed else result


@mcp.tool()
def remove_source(source_name: str, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Remove a source: its sources.yml entry, staging files, config.toml section and monitors.

    Always call with dry_run=True first and show the user downstream_models, monitors_removed
    and files_removed. force=True is required when models depend on the source. Warehouse data
    and .env are not touched (env_vars_matching lists names the user may want to delete).
    """
    project_root = _get_project_root()
    try:
        from dango.ingestion.sources.setup_lifecycle import remove_source as _remove
        from dango.ingestion.sources.setup_schema import SourceSetupError

        try:
            r = _remove(project_root, source_name, force=force, dry_run=dry_run)
        except SourceSetupError as e:
            return _service_error(e)
    except Exception as e:  # noqa: BLE001
        return {"error": f"remove_source failed: {type(e).__name__}: {e}"}
    result = dataclasses.asdict(r)
    return result if dry_run else _with_git_warning(result, project_root)


@mcp.tool()
def validate_source(source_name: str, check_connectivity: bool = False) -> dict[str, Any]:
    """Check whether a source is ready to sync (read-only; never returns secret values).

    Reports missing settings, unset credentials, and (for file sources) matching files. With
    check_connectivity=True also validates OAuth tokens against the provider.

    Returns dict with: source_name, type, enabled, ready, issues, files_found.
    """
    project_root = _get_project_root()
    try:
        from dango.config.helpers import load_config
        from dango.config.models import DataSource
        from dango.ingestion.sources.setup_lifecycle import credential_requirements

        source = load_config(project_root).sources.get_source(source_name)
        if source is None:
            return {"error": f"Source '{source_name}' not found"}
        st = source.type.value
        issues: list[str] = []
        files_found: int | None = None
        block = getattr(source, st, None) if st in DataSource.model_fields else None
        if st in DataSource.model_fields and block is None:
            issues.append(f"{st} config missing — call update_source with the required settings")
        if st in ("local_files", "csv") and block is not None:
            directory = project_root / block.directory
            from dango.ingestion.csv_loader import SUPPORTED_READ_FUNCTIONS

            files_found = (
                sum(
                    1
                    for p in directory.glob(block.file_pattern)
                    if p.is_file() and p.suffix.lower() in SUPPORTED_READ_FUNCTIONS
                )
                if directory.is_dir()
                else 0
            )
            if files_found == 0:
                issues.append(f"No files matching '{block.file_pattern}' in {block.directory}")
        for req in credential_requirements(project_root, source):
            issues.append(req.detail)
        if check_connectivity:
            issues.extend(_connectivity_issues(st, project_root))
    except Exception as e:  # noqa: BLE001
        return {"error": f"validate_source failed: {type(e).__name__}: {e}"}
    return {
        "source_name": source_name,
        "type": st,
        "enabled": source.enabled,
        "ready": not issues,
        "issues": issues,
        "files_found": files_found,
    }


def _connectivity_issues(source_type: str, project_root: Path) -> list[str]:
    from dango.oauth.validation import validate_before_sync

    try:
        validate_before_sync(source_type, project_root)
    except Exception as e:  # noqa: BLE001
        return [str(getattr(e, "user_message", None) or e)]
    return []
