"""dango/transformation/model_docs.py

schema.yml helpers for dbt models: locate, upsert and remove a model's docs entry while preserving every other key.
"""

import copy
from pathlib import Path
from typing import Any

import yaml

from dango.transformation.model_common import ModelServiceError


def _err(errors: list[str]) -> Exception:
    return ModelServiceError(errors)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a yml file as a dict; invalid YAML or a non-mapping raises ModelServiceError."""
    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError) as e:
        raise _err([f"{path.name} is not valid YAML: {e}"]) from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _err([f"{path.name} must contain a YAML mapping at the top level"])
    return data


def _model_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    models = data.get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, dict)]


def locate_model_docs(project_root: Path, model_name: str) -> Path | None:
    """Return the yml file under dbt/models/ whose ``models:`` list holds model_name.

    Two files holding the same model is an error (dbt rejects duplicate patches).
    Files that are not valid YAML are skipped here; they surface when edited.
    """
    models_dir = project_root / "dbt" / "models"
    if not models_dir.exists():
        return None
    found: list[Path] = []
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(models_dir.rglob(pattern)):
            try:
                data = yaml.safe_load(path.read_text())
            except (yaml.YAMLError, OSError):
                continue
            if isinstance(data, dict) and any(
                m.get("name") == model_name for m in _model_entries(data)
            ):
                found.append(path)
    if len(found) > 1:
        names = ", ".join(str(p.relative_to(project_root)) for p in found)
        raise _err([f"Model '{model_name}' is documented in multiple files: {names}"])
    return found[0] if found else None


def default_docs_path(project_root: Path, layer: str, model_name: str) -> Path:
    """Where a model's docs go when no yml holds it yet."""
    models_dir = project_root / "dbt" / "models"
    if layer == "staging" and model_name.startswith("stg_"):
        source = model_name[len("stg_") :].split("__", 1)[0]
        return models_dir / "staging" / f"stg_{source}.yml"
    return models_dir / layer / "schema.yml"


def resolve_docs_path(project_root: Path, layer: str, model_name: str) -> Path:
    """Existing docs file for the model, else the default location."""
    return locate_model_docs(project_root, model_name) or default_docs_path(
        project_root, layer, model_name
    )


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def validate_column_entries(columns: list[dict[str, Any]] | None) -> None:
    """Reject malformed column entries, including both `tests` and `data_tests` on one column."""
    for col in columns or []:
        if not isinstance(col, dict) or not col.get("name"):
            raise _err(["every column entry must be a mapping with a 'name'"])
        if "tests" in col and "data_tests" in col:
            raise _err([f"column '{col['name']}': pass either 'data_tests' or 'tests', not both"])


def upsert_model_docs(
    project_root: Path,
    layer: str,
    model_name: str,
    *,
    description: str | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> str | None:
    """Set a model's description and merge column docs; preserve everything else.

    Returns the project-relative path if the file changed, else None.
    """
    validate_column_entries(columns)

    path = resolve_docs_path(project_root, layer, model_name)
    existed = path.exists()
    data = _load_yaml(path) if existed else {"version": 2, "models": []}
    original = copy.deepcopy(data) if existed else None
    data.setdefault("version", 2)
    if not isinstance(data.get("models"), list):
        data["models"] = []

    entry = next((m for m in _model_entries(data) if m.get("name") == model_name), None)
    if entry is None:
        entry = {"name": model_name}
        data["models"].append(entry)
    if description is not None:
        entry["description"] = description
    if columns:
        existing_cols = entry.get("columns")
        if not isinstance(existing_cols, list):
            existing_cols = []
        for col in columns:
            target = next(
                (c for c in existing_cols if isinstance(c, dict) and c.get("name") == col["name"]),
                None,
            )
            if target is None:
                existing_cols.append(dict(col))
            else:
                if "data_tests" in col or "tests" in col:  # dbt rejects both keys on one column
                    target.pop("tests", None)
                    target.pop("data_tests", None)
                target.update(col)
        entry["columns"] = existing_cols

    if existed and data == original:
        return None
    _write_yaml(path, data)
    return str(path.relative_to(project_root))


def remove_model_docs(project_root: Path, layer: str, model_name: str) -> str | None:
    """Remove a model's docs entry, preserving everything else.

    The file is deleted only when no models and no other top-level keys remain.
    Returns the project-relative path if changed.
    """
    path = locate_model_docs(project_root, model_name)
    if path is None:
        return None
    data = _load_yaml(path)
    models = data["models"]
    data["models"] = [
        m for m in models if not (isinstance(m, dict) and m.get("name") == model_name)
    ]
    rel = str(path.relative_to(project_root))
    if not data["models"] and set(data) <= {"version", "models"}:
        path.unlink()
    else:
        _write_yaml(path, data)
    return rel
