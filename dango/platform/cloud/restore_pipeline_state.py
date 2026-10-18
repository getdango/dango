"""dango/platform/cloud/restore_pipeline_state.py

Local-restore helpers resetting dlt's local pipeline state for a project's own pipelines.
"""

from __future__ import annotations

import time
from pathlib import Path


def pipeline_names(project_root: Path) -> list[str] | None:
    """Pipeline names of the project's sources (``None`` if sources.yml cannot be loaded)."""
    import yaml

    from dango.config.loader import ConfigLoader
    from dango.exceptions import DangoError

    try:
        sources = ConfigLoader(project_root).load_sources_config().sources
    except (DangoError, yaml.YAMLError, OSError):
        return None
    return [
        (src.dlt_native.pipeline_name if src.dlt_native else None) or src.name for src in sources
    ]


def set_aside_pipeline_state(names: list[str]) -> list[Path]:
    """Move the local dlt state of pipelines *names* aside, next to themselves.

    dlt keeps it in ``<DLT_DATA_DIR or ~>/.dlt/pipelines/<name>``, a directory shared by
    every project on the machine, so only these pipelines are touched (links are renamed,
    never followed).  A restored warehouse older than that state would otherwise never
    reload the rows loaded in between; without it dlt re-derives the state from the
    warehouse.  The target is ``<name>.pre-restore-<stamp>`` (numbered if taken).
    Returns the new locations.
    """
    from dango.ingestion.dlt_runner import _dlt_pipelines_dir

    root = _dlt_pipelines_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: list[Path] = []
    for name in sorted(set(names)):
        if Path(name).name != name or name in ("", ".", ".."):
            continue
        live = root / name
        if not (live.exists() or live.is_symlink()):
            continue
        kept = root / f"{name}.pre-restore-{stamp}"
        n = 0
        while kept.exists() or kept.is_symlink():
            n += 1
            kept = root / f"{name}.pre-restore-{stamp}-{n}"
        live.replace(kept)
        moved.append(kept)
    return moved
