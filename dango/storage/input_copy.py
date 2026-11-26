"""dango/storage/input_copy.py

Materialize selected admitted snapshots through authorized bindings and measured native files.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from dango.operations.contracts import OperationContext
from dango.storage.catalog_ownership import catalog_file_paths
from dango.storage.catalogs import (
    CatalogAllocator,
    CatalogBinding,
    _credential,
    _dsn,
    _literal,
    _quote,
)
from dango.storage.input_state import (
    AcceptedFixture,
    Column,
    InputCaptureError,
    Relation,
    StageFile,
    directory,
    fingerprint,
)
from dango.storage.workspace_state import _owned

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def qualified(relation: Relation) -> str:
    """Quote only the original server-resolved identifiers, never caller SQL."""
    return "catalog." + _quote(relation.schema_name) + "." + _quote(relation.table_name)


class NativeInputCopy:
    """Keep source and target allocator locks separate; current authorization gates each use."""

    def __init__(self, root: Path, allocator: CatalogAllocator):
        self.root, self.allocator = root, allocator

    def _credential(self, context: OperationContext, binding: CatalogBinding) -> dict[str, str]:
        if self.allocator.authorize(context, binding, "read") is not True:
            raise InputCaptureError("capture_denied")
        return _credential(self.allocator.resolve(binding.credential_ref), binding.catalog_database)

    def _schema(self, connection: DuckDBPyConnection, relation: Relation) -> None:
        rows = connection.execute("DESCRIBE SELECT * FROM " + qualified(relation)).fetchall()
        columns = tuple(
            Column(name=str(r[0]), type=str(r[1]), nullable=r[2] == "YES") for r in rows
        )
        if columns != relation.columns or fingerprint(columns) != relation.schema_fingerprint:
            raise InputCaptureError("capture_schema_changed")

    def _bytes(
        self,
        context: OperationContext,
        binding: CatalogBinding,
        relation: Relation,
        snapshot: int | None,
    ) -> tuple[str, int]:
        credential = self._credential(context, binding)
        schema = _quote(binding.metadata_schema)
        port = self.allocator.port
        with port._postgres(credential) as connection:
            tables = port._query(
                connection,
                "SELECT t.table_uuid::text FROM "
                + schema
                + ".ducklake_table t JOIN "
                + schema
                + ".ducklake_schema s USING(schema_id) WHERE s.schema_name="
                + _literal(relation.schema_name)
                + " AND t.table_name="
                + _literal(relation.table_name),
            )
            if len(tables) != 1:
                raise InputCaptureError("capture_source_unavailable")
            total, seen = 0, set()
            for table in ("ducklake_data_file", "ducklake_delete_file"):
                active = (
                    "f.end_snapshot IS NULL"
                    if snapshot is None
                    else (
                        f"f.begin_snapshot<={snapshot} AND (f.end_snapshot IS NULL OR f.end_snapshot>{snapshot})"
                    )
                )
                rows = port._query(
                    connection,
                    "SELECT s.path,s.path_is_relative,t.path,t.path_is_relative,f.path,"
                    "f.path_is_relative,f.file_size_bytes FROM "
                    + schema
                    + "."
                    + table
                    + " f JOIN "
                    + schema
                    + ".ducklake_table t USING(table_id) JOIN "
                    + schema
                    + ".ducklake_schema s USING(schema_id) WHERE t.table_uuid::text="
                    + _literal(str(tables[0][0]))
                    + " AND "
                    + active,
                )
                for row in rows:
                    files = catalog_file_paths([(*row[:6], None)], Path(binding.data_root_ref))
                    if len(files) != 1 or type(row[6]) is not int or row[6] < 0:
                        raise InputCaptureError("capture_source_unavailable")
                    path = next(iter(files))
                    if (Path(binding.data_root_ref) / path).stat().st_size != row[6]:
                        raise InputCaptureError("capture_source_unavailable")
                    if path not in seen:
                        seen.add(path)
                        total += row[6]
            return str(tables[0][0]), total

    def preflight(
        self, context: OperationContext, binding: CatalogBinding, relations: tuple[Relation, ...]
    ) -> None:
        """Refuse any selected target schema/table collision before exporting source data."""
        with self.allocator.attach(context, binding, read_only=False) as connection:
            self._collisions(connection, relations)

    @staticmethod
    def _collisions(connection: DuckDBPyConnection, relations: tuple[Relation, ...]) -> None:
        schemas = {
            r[0]
            for r in connection.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE catalog_name='catalog'"
            ).fetchall()
        }
        if any(relation.schema_name in schemas for relation in relations):
            raise InputCaptureError("capture_collision")

    def export(
        self,
        context: OperationContext,
        manifest: AcceptedFixture,
        relations: tuple[Relation, ...],
        input_set_id: str,
        max_bytes: int,
        gate: Callable[[], None],
        estimate_ready: Callable[[int], None],
    ) -> int:
        """Hold the source allocator lock while reading the exact native snapshot into owned staging."""
        binding = manifest.binding
        stage_fd = directory(self.root, "staging", input_set_id)
        os.close(stage_fd)
        stage = self.root / ".dango/state/inputs/staging" / input_set_id
        gate()
        with self.allocator.attach(context, binding, read_only=True) as connection:
            credential = self._credential(context, binding)
            connection.execute("DETACH catalog")
            options = (
                "METADATA_SCHEMA "
                + _literal(binding.metadata_schema)
                + ", DATA_PATH "
                + _literal(binding.data_root_ref)
                + ", DUCKLAKE_VERSION '1.1', AUTOMATIC_MIGRATION FALSE, "
                "DATA_INLINING_ROW_LIMIT 0, READ_ONLY, SNAPSHOT_VERSION "
                + str(manifest.snapshot_id)
            )
            try:
                connection.execute(
                    "ATTACH "
                    + _literal("ducklake:postgres:" + _dsn(credential))
                    + " AS catalog ("
                    + options
                    + ")"
                )
            except Exception:
                raise InputCaptureError("capture_source_unavailable") from None

            gate()
            estimate = 0
            for relation in relations:
                self._schema(connection, relation)
                uuid, size = self._bytes(context, binding, relation, manifest.snapshot_id)
                if uuid != relation.table_uuid:
                    raise InputCaptureError("capture_schema_changed")
                estimate += size
            estimate_ready(estimate)
            if estimate > max_bytes:
                raise InputCaptureError("capture_quota_exceeded")
            for index, relation in enumerate(relations):
                path = stage / f"{index}.parquet"
                if path.exists() or path.is_symlink():
                    raise InputCaptureError("capture_recovery_required")
                connection.execute(
                    "COPY (SELECT * FROM "
                    + qualified(relation)
                    + ") TO "
                    + _literal(str(path))
                    + " (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                path.chmod(0o600)
                self._schema(connection, relation)
            gate()
        gate()
        return estimate

    def inventory(self, input_set_id: str, count: int) -> tuple[StageFile, ...]:
        """Persist inode identities only after export completes; unknown residuals stay retained."""
        fd = directory(self.root, "staging", input_set_id)
        files = []
        try:
            for index in range(count):
                item = os.open(
                    f"{index}.parquet", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
                try:
                    info = _owned(item)
                    files.append(
                        StageFile(
                            index=index, device=info.st_dev, inode=info.st_ino, size=info.st_size
                        )
                    )
                finally:
                    os.close(item)
        finally:
            os.close(fd)
        return tuple(files)

    def cleanup(self, input_set_id: str, files: tuple[StageFile, ...]) -> None:
        """Remove only recorded unchanged staging inodes; permit partial cleanup retries."""
        fd = directory(self.root, "staging", input_set_id)
        try:
            for file in files:
                name = f"{file.index}.parquet"
                try:
                    item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                except FileNotFoundError:
                    continue
                try:
                    info = _owned(item)
                    if (info.st_dev, info.st_ino, info.st_size) != (
                        file.device,
                        file.inode,
                        file.size,
                    ):
                        raise InputCaptureError("capture_recovery_required")
                    os.unlink(name, dir_fd=fd)
                finally:
                    os.close(item)
            os.fsync(fd)
        finally:
            os.close(fd)

    def copy_one(
        self,
        context: OperationContext,
        binding: CatalogBinding,
        relation: Relation,
        input_set_id: str,
        index: int,
    ) -> tuple[str, int]:
        """Commit one owned relation, then measure visible native retained bytes; residuals may remain."""
        stage = self.root / ".dango/state/inputs/staging" / input_set_id / f"{index}.parquet"
        with self.allocator.attach(context, binding, read_only=False) as connection:
            # A preceding selected relation may have created this schema in this capture.
            exists = connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_catalog='catalog' "
                "AND table_schema=? AND table_name=?",
                [relation.schema_name, relation.table_name],
            ).fetchone()
            if exists is None or exists[0] != 0:
                raise InputCaptureError("capture_collision")
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "CREATE SCHEMA IF NOT EXISTS catalog." + _quote(relation.schema_name)
                )
                if any(column.type not in {"INTEGER", "BIGINT"} for column in relation.columns):
                    raise InputCaptureError("capture_schema_changed")
                columns = ",".join(
                    _quote(c.name) + " " + c.type + ("" if c.nullable else " NOT NULL")
                    for c in relation.columns
                )
                connection.execute("CREATE TABLE " + qualified(relation) + " (" + columns + ")")
                connection.execute(
                    "INSERT INTO "
                    + qualified(relation)
                    + " SELECT * FROM read_parquet("
                    + _literal(str(stage))
                    + ")"
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            self._schema(connection, relation)
        return self._bytes(context, binding, relation, None)
