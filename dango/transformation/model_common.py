"""dango/transformation/model_common.py

Shared types, naming rules and model lookup for the dbt model service.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MODEL_LAYERS = ("staging", "intermediate", "marts")
CUSTOM_MODEL_LAYERS = ("intermediate", "marts")
_VALID_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")  # same rule as commands/model.py


class ModelServiceError(Exception):
    """Validation failed; nothing was written."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ModelChangeResult:
    """Outcome of a create/update/remove (or its dry run)."""

    model_name: str
    layer: str
    status: str  # "created" | "updated" | "removed" | "dry_run"
    path: str | None  # project-relative .sql path
    files_changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # ref() targets in the SQL
    sources: list[str] = field(default_factory=list)  # "source_name.table" from source()
    downstream: list[str] = field(default_factory=list)  # models that ref this one
    table_existed: bool = False
    dropped_table: bool = False
    parse_ok: bool | None = None  # None = parse not run
    parse_output: str | None = None  # last ~4000 chars of dbt parse output on failure
    monitors_removed: list[str] = field(default_factory=list)  # monitor names referencing it


def normalize_model_name(name: str, layer: str) -> str:
    """Normalise a model name for a layer; returns the name without ``.sql``."""
    if layer not in MODEL_LAYERS:
        raise ModelServiceError([f"Unknown layer '{layer}'; expected one of {MODEL_LAYERS}"])
    result = name.strip().lower().removesuffix(".sql")
    if layer == "intermediate":
        result = "int_" + result.removeprefix("int_")
    elif layer == "staging" and not result.startswith("stg_"):
        raise ModelServiceError([f"Staging model names must start with 'stg_': '{result}'"])
    if not _VALID_MODEL_NAME_RE.match(result):
        raise ModelServiceError(
            [
                f"Invalid model name '{result}': must be lowercase, start with a letter, "
                "and contain only letters, digits and underscores"
            ]
        )
    return result


def iter_model_files(project_root: Path) -> list[tuple[Path, str]]:
    """All (path, layer) model files under dbt/models/, recursive."""
    models_dir = project_root / "dbt" / "models"
    if not models_dir.exists():
        return []
    found: list[tuple[Path, str]] = []
    for path in sorted(models_dir.rglob("*.sql")):
        layer = path.relative_to(models_dir).parts[0]
        if layer in MODEL_LAYERS and len(path.relative_to(models_dir).parts) > 1:
            found.append((path, layer))
    return found


def find_model(project_root: Path, model_name: str) -> tuple[Path, str] | None:
    """Find ``dbt/models/**/{model_name}.sql`` (recursive); returns (absolute path, layer)."""
    matches = [(p, layer) for p, layer in iter_model_files(project_root) if p.stem == model_name]
    if len(matches) > 1:
        paths = ", ".join(str(p.relative_to(project_root)) for p, _ in matches)
        raise ModelServiceError(
            [f"Model name '{model_name}' is not unique (dbt requires unique names): {paths}"]
        )
    return matches[0] if matches else None


def list_models(project_root: Path) -> dict[str, str]:
    """Every model name -> layer, recursive, skipping files that start with ``_``."""
    return {p.stem: layer for p, layer in iter_model_files(project_root) if p.name[0] != "_"}


def stems(project_root: Path, subdir: str, pattern: str) -> set[str]:
    """File stems matching pattern under dbt/{subdir}/ (seeds, snapshots)."""
    base = project_root / "dbt" / subdir
    return {p.stem for p in base.rglob(pattern)} if base.exists() else set()


def rel_path(project_root: Path, path: Path) -> str:
    """Project-relative string form of path."""
    return str(path.relative_to(project_root))
