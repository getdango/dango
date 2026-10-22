"""dango/visualization/collection_repair.py

Path-based Metabase collection import planning and archive-only repair of empty duplicates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeGuard

FALLBACK_COLLECTION_ID = 1

CollectionPath = tuple[str, ...]


class CollectionExportError(ValueError):
    """The exported collection hierarchy is invalid; nothing was changed."""


class CollectionApiError(RuntimeError):
    """Metabase could not be queried; no collection was changed and the import retries later."""


@dataclass(frozen=True)
class ExportedPath:
    """One normalized exported path with every source ID that resolves to it."""

    path: CollectionPath
    source_ids: tuple[int, ...]

    @property
    def parent(self) -> CollectionPath | None:
        """Parent path, or None for a root collection."""
        return self.path[:-1] or None


@dataclass(frozen=True)
class LiveCollection:
    """An active, non-personal live collection and its full sibling path."""

    id: int
    name: str
    parent_id: int | None
    path: CollectionPath


@dataclass
class LiveTree:
    """Active live collections indexed by full path, plus active child counts per ID."""

    by_path: dict[CollectionPath, list[LiveCollection]] = field(default_factory=dict)
    child_counts: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Selection:
    """Outcome of choosing among duplicate candidates at one exact path."""

    canonical: int
    archive: tuple[int, ...] = ()
    ambiguous: tuple[int, ...] = ()
    error: str | None = None


@dataclass
class PlanStep:
    """One exported path and its selection (None means the path must be created)."""

    entry: ExportedPath
    selection: Selection | None  # None means: create the collection


@dataclass
class RepairResult:
    """Everything an import did or planned for collections, for output, logs and the report."""

    id_mapping: dict[int, int] = field(default_factory=dict)
    created_ids: list[int] = field(default_factory=list)
    archived_ids: list[int] = field(default_factory=list)
    ambiguous_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    kept_ids: dict[str, int] = field(default_factory=dict)
    planned: list[str] = field(default_factory=list)
    fingerprint: str = ""
    dry_run: bool = False
    report_path: str | None = None

    @property
    def complete(self) -> bool:
        """True only when nothing failed and no ambiguous duplicate remains."""
        return not self.errors and not self.ambiguous_ids

    @property
    def mutated(self) -> bool:
        """True when the run created or archived any collection."""
        return bool(self.created_ids or self.archived_ids)

    def as_summary(self) -> dict[str, Any]:
        """JSON-friendly summary placed on the import result."""
        return {
            "dry_run": self.dry_run,
            "created": len(self.created_ids),
            "created_ids": list(self.created_ids),
            "archived": len(self.archived_ids),
            "archived_ids": list(self.archived_ids),
            "ambiguous_ids": list(self.ambiguous_ids),
            "errors": list(self.errors),
            "planned": list(self.planned),
            "complete": self.complete,
            "report_path": self.report_path,
        }


class CollectionApi(Protocol):
    """The Metabase calls the executor needs; HTTP lives outside the planner."""

    def list_collections(self) -> list[dict[str, Any]]:
        """Active collections (raises on failure)."""
        ...

    def count_items(self, collection_id: int) -> int | None:
        """Item count, or None when it cannot be determined."""
        ...

    def create(self, name: str, parent_id: int | None) -> int:
        """Create a collection and return its ID."""
        ...

    def archive(self, collection_id: int) -> None:
        """Archive (never delete) a collection."""
        ...


# ---------------------------------------------------------------------------
# Pure planning helpers (no HTTP)
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_export(collections: Sequence[Mapping[str, Any]]) -> list[ExportedPath]:
    """Validate exported rows and return unique paths, parents before children."""
    rows: dict[int, Mapping[str, Any]] = {}
    for col in collections:
        if not _is_int(col.get("id")):
            raise CollectionExportError(f"Collection missing or invalid 'id' (expected int): {col}")
        if not isinstance(col.get("name"), str) or not col["name"]:
            raise CollectionExportError(
                f"Collection missing or invalid 'name' (expected str): {col}"
            )
        parent_id = col.get("parent_id")
        if parent_id is not None and not _is_int(parent_id):
            raise CollectionExportError(
                f"Collection '{col['name']}' has invalid 'parent_id' "
                f"(expected int or None, got {type(parent_id).__name__}): {col}"
            )
        previous = rows.get(col["id"])
        if previous is not None and (
            previous["name"] != col["name"] or previous.get("parent_id") != parent_id
        ):
            raise CollectionExportError(f"Collection id {col['id']} is defined twice differently")
        rows[col["id"]] = col

    paths: dict[int, CollectionPath] = {}

    def resolve(cid: int) -> CollectionPath:
        """Path of one exported row; raises on missing parents and cycles."""
        chain: list[int] = []
        current: int | None = cid
        while current is not None and current not in paths:
            if current in chain:
                raise CollectionExportError(f"Cyclic parent reference at collection id {current}")
            if current not in rows:
                raise CollectionExportError(
                    f"Collection id {chain[-1]} references missing parent id {current}"
                )
            chain.append(current)
            current = rows[current].get("parent_id")
        base: CollectionPath = paths[current] if current is not None else ()
        for node in reversed(chain):
            base = (*base, rows[node]["name"])
            paths[node] = base
        return paths[cid]

    for cid in rows:
        resolve(cid)

    grouped: dict[CollectionPath, list[int]] = {}
    for cid in rows:
        grouped.setdefault(paths[cid], []).append(cid)
    ordered = sorted(grouped, key=lambda p: len(p))  # stable: keeps export order per depth
    return [ExportedPath(path=p, source_ids=tuple(grouped[p])) for p in ordered]


def export_fingerprint(entries: Sequence[ExportedPath]) -> str:
    """Stable hash of the exported path set, recorded in the repair report."""
    payload = json.dumps([list(e.path) for e in entries], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parent_from_location(location: Any) -> tuple[int | None, bool]:
    """Return (parent id, valid). Metabase location is ``/`` or ``/<ancestor>/.../<parent>/``."""
    if not isinstance(location, str) or not location.startswith("/"):
        return None, False
    parts = [p for p in location.split("/") if p]
    if not parts:
        return None, True
    if not all(p.isdigit() for p in parts):
        return None, False
    return int(parts[-1]), True


def index_live(raw: Sequence[Mapping[str, Any]]) -> LiveTree:
    """Index active collections by full path. Anything malformed or unreachable is skipped."""
    nodes: dict[int, tuple[str, int | None]] = {}
    for item in raw:
        cid = item.get("id")
        name = item.get("name")
        if not _is_int(cid) or not isinstance(name, str):
            continue  # also skips the virtual "root" entry
        if item.get("archived") is True or item.get("personal_owner_id") is not None:
            continue
        parent_id, valid = _parent_from_location(item.get("location"))
        if not valid:
            continue
        nodes[cid] = (name, parent_id)

    tree = LiveTree()
    paths: dict[int, CollectionPath | None] = {}

    def path_of(cid: int) -> CollectionPath | None:
        """Full live path, or None when an ancestor is missing, archived or cyclic."""
        if cid in paths:
            return paths[cid]
        paths[cid] = None  # cycle guard
        name, parent_id = nodes[cid]
        if parent_id is None:
            result: CollectionPath | None = (name,)
        elif parent_id in nodes:
            parent_path = path_of(parent_id)
            result = (*parent_path, name) if parent_path else None
        else:
            result = None
        paths[cid] = result
        return result

    for cid in sorted(nodes):
        path = path_of(cid)
        if path is None:
            continue
        name, parent_id = nodes[cid]
        tree.by_path.setdefault(path, []).append(LiveCollection(cid, name, parent_id, path))
        if parent_id is not None:
            tree.child_counts[parent_id] = tree.child_counts.get(parent_id, 0) + 1
    return tree


def ids_needing_inventory(entries: Sequence[ExportedPath], tree: LiveTree) -> list[int]:
    """Candidate IDs whose contents must be inspected: only paths with several candidates."""
    wanted: list[int] = []
    for entry in entries:
        candidates = tree.by_path.get(entry.path, [])
        if len(candidates) > 1:
            wanted.extend(c.id for c in candidates)
    return wanted


def select_canonical(
    candidates: Sequence[LiveCollection],
    source_ids: Collection[int],
    item_counts: Mapping[int, int | None],
    child_counts: Mapping[int, int],
) -> Selection:
    """Pick the canonical candidate and the empties that are provably safe to archive."""
    ordered = sorted(candidates, key=lambda c: c.id)
    ids = [c.id for c in ordered]

    def pick(pool: Sequence[int]) -> int:
        """Exported-ID candidate if present, else the lowest ID."""
        return next((i for i in pool if i in source_ids), pool[0])

    if len(ordered) == 1:
        return Selection(canonical=ids[0])

    def items_of(cid: int) -> int | None:
        """Known item count or None."""
        count = item_counts.get(cid)
        return count if _is_int(count) and count >= 0 else None

    unknown = [i for i in ids if items_of(i) is None and child_counts.get(i, 0) == 0]
    nonempty = [i for i in ids if i not in unknown and (items_of(i) or child_counts.get(i, 0) > 0)]
    empty = [i for i in ids if i not in unknown and i not in nonempty]

    if unknown:
        canonical = pick(nonempty or ids)
        return Selection(
            canonical=canonical,
            ambiguous=tuple(i for i in ids if i != canonical),
            error=(
                f"Could not inspect contents of duplicate collection(s) {unknown} at "
                f"{'/'.join(ordered[0].path)}; nothing was archived (will retry on next start)"
            ),
        )
    if nonempty:
        canonical = pick(nonempty)
        return Selection(
            canonical=canonical,
            archive=tuple(i for i in empty if i != canonical),
            ambiguous=tuple(i for i in nonempty if i != canonical),
        )
    canonical = pick(ids)
    return Selection(canonical=canonical, archive=tuple(i for i in ids if i != canonical))


def plan_steps(
    entries: Sequence[ExportedPath],
    tree: LiveTree,
    item_counts: Mapping[int, int | None],
) -> list[PlanStep]:
    """Pure plan: reuse/repair the live candidates for each exported path, or create."""
    steps: list[PlanStep] = []
    for entry in entries:
        candidates = tree.by_path.get(entry.path, [])
        selection = (
            select_canonical(candidates, entry.source_ids, item_counts, tree.child_counts)
            if candidates
            else None
        )
        steps.append(PlanStep(entry=entry, selection=selection))
    return steps


# ---------------------------------------------------------------------------
# Execution (HTTP mutation kept apart from planning)
# ---------------------------------------------------------------------------


def _label(path: CollectionPath) -> str:
    return "/".join(path)


def repair_and_map(
    api: CollectionApi,
    collections: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool = False,
    result: RepairResult | None = None,
) -> RepairResult:
    """Resolve every exported path to one live collection, creating or repairing as needed.

    Raises CollectionExportError (invalid export) or CollectionApiError (Metabase unreachable)
    before any mutation. After that point failures are recorded in ``result.errors``.
    """
    result = result if result is not None else RepairResult()
    result.dry_run = dry_run
    entries = normalize_export(collections)
    result.fingerprint = export_fingerprint(entries)
    if not entries:
        return result
    try:
        tree = index_live(api.list_collections())
    except Exception as exc:
        raise CollectionApiError(
            f"Could not list Metabase collections ({exc}); collection import and repair "
            "will retry on the next start"
        ) from exc

    counts: dict[int, int | None] = {}
    for cid in ids_needing_inventory(entries, tree):
        try:
            counts[cid] = api.count_items(cid)
        except Exception:
            counts[cid] = None

    path_ids: dict[CollectionPath, int] = {}
    failed: dict[CollectionPath, int] = {}  # create failed: descendants map to the same fallback
    for step in plan_steps(entries, tree, counts):
        entry, selection = step.entry, step.selection
        label = _label(entry.path)
        if selection is None:
            new_id = _create(api, entry, path_ids, failed, result, label)
            if new_id is None:
                continue
            path_ids[entry.path] = new_id
            for sid in entry.source_ids:
                result.id_mapping[sid] = new_id
            result.kept_ids[label] = new_id
            continue
        path_ids[entry.path] = selection.canonical
        result.kept_ids[label] = selection.canonical
        for sid in entry.source_ids:
            result.id_mapping[sid] = selection.canonical
        _apply_selection(api, selection, label, result, dry_run)
    return result


def _create(
    api: CollectionApi,
    entry: ExportedPath,
    path_ids: dict[CollectionPath, int],
    failed: dict[CollectionPath, int],
    result: RepairResult,
    label: str,
) -> int | None:
    parent = entry.parent
    if parent is not None and parent in failed:
        fallback = failed[parent]
        failed[entry.path] = fallback
        result.errors.append(f"Skipped '{label}': parent collection could not be created")
        for sid in entry.source_ids:
            result.id_mapping[sid] = fallback
        return None
    parent_id = path_ids.get(parent) if parent is not None else None
    if result.dry_run:
        result.planned.append(f"create collection '{label}'")
        fake = -(len(result.planned))
        path_ids[entry.path] = fake
        for sid in entry.source_ids:
            result.id_mapping[sid] = fake
        return None
    try:
        new_id = api.create(entry.path[-1], parent_id)
    except Exception as exc:
        fallback = parent_id if parent_id else FALLBACK_COLLECTION_ID
        failed[entry.path] = fallback
        result.errors.append(f"Failed to create collection '{label}': {exc}")
        for sid in entry.source_ids:
            result.id_mapping[sid] = fallback
        return None
    result.created_ids.append(new_id)
    return new_id


def _apply_selection(
    api: CollectionApi, selection: Selection, label: str, result: RepairResult, dry_run: bool
) -> None:
    if selection.error:
        result.errors.append(selection.error)
    for cid in selection.ambiguous:
        if cid not in result.ambiguous_ids:
            result.ambiguous_ids.append(cid)
    if selection.ambiguous and not selection.error:
        result.errors.append(
            f"Ambiguous duplicate collection(s) {list(selection.ambiguous)} at '{label}' contain "
            f"items; kept {selection.canonical}, archived nothing automatically"
        )
    for cid in selection.archive:
        if dry_run:
            result.planned.append(f"archive empty duplicate collection {cid} at '{label}'")
            continue
        try:
            api.archive(cid)
        except Exception as exc:
            result.errors.append(
                f"Failed to archive duplicate collection {cid} at '{label}': {exc}"
            )
            continue
        result.archived_ids.append(cid)
