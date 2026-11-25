"""dango/storage/catalogs.py

Authorize current catalog bindings and join private ownership to concrete DuckLake operations.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from dango.operations.contracts import OperationContext
from dango.security.native_credentials import SecretRef
from dango.storage.catalog_ownership import (
    OwnedWorkspace,
    canonical_path,
    catalog_file_paths,
    opaque_identifier,
    restrict_drop_sql,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


class CatalogError(RuntimeError):
    """Expose only a fixed safe error; native exception chains never cross this boundary."""

    def __init__(self) -> None:
        self.code = "catalog_operation_denied"
        self.action = "Ask an administrator to verify current access and catalog ownership."
        super().__init__(f"{self.code}: {self.action}")


_ID = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(opaque_identifier)]
Action = Literal["allocate", "read", "write", "reconcile", "remove"]


class CatalogBinding(BaseModel):
    """Reference persisted ownership; record construction never grants native authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    binding_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    project_id: _ID
    workspace_id: _ID
    owner_actor_id: _ID
    catalog_database: _ID
    metadata_schema: Annotated[str, Field(pattern=r"^dango_[a-f0-9]{32}$")]
    data_root_ref: Annotated[str, AfterValidator(canonical_path)]
    credential_ref: SecretRef
    bundle_id: _ID
    state: Literal["allocating", "ready", "recovery_required"]
    revision: Annotated[int, Field(ge=0)]


def _literal(value: str) -> str:
    if "\x00" in value:
        raise CatalogError()
    return "'" + value.replace("'", "''") + "'"


def _quote(value: str) -> str:
    if "\x00" in value:
        raise CatalogError()
    return '"' + value.replace('"', '""') + '"'


def _credential(value: object, database: str) -> dict[str, str]:
    fields = {"host", "port", "dbname", "user", "password"}
    if type(value) is not dict or value.keys() != fields:
        raise CatalogError()
    if any(type(v) is not str or not v or "\x00" in v for v in value.values()):
        raise CatalogError()
    if not re.fullmatch(r"[0-9]{1,5}", value["port"]) or not 1 <= int(value["port"]) <= 65535:
        raise CatalogError()
    if value["dbname"] != database:
        raise CatalogError()
    return dict(value)


def _dsn(credential: dict[str, str]) -> str:
    return " ".join(
        key + "='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        for key, value in sorted(credential.items())
    )


def _dependencies(relations: list[Any]) -> str:
    oids = ",".join(str(row[0]) for row in relations)
    if not oids:
        return "SELECT ''::text"
    return (
        "SELECT coalesce(string_agg(classid::text||':'||objid::text||':'||objsubid::text||':'||refobjid::text||':'||refobjsubid::text||':'||deptype::text,',' ORDER BY classid,objid,objsubid,refobjid,refobjsubid,deptype),'') FROM pg_depend WHERE refclassid='pg_class'::regclass AND refobjid IN ("
        + oids
        + ")"
    )


class NativeCatalogPort:
    """Concrete exact-pin development adapter; no profile admission or SQL sandbox."""

    def __init__(self, extension_directory: Path):
        self.extensions = extension_directory

    def _connect(self) -> DuckDBPyConnection:
        import duckdb

        if duckdb.__version__ != "2.0.0":
            raise CatalogError()
        expected = {
            "ducklake": "4b497cf95d768004d34a97e7098d33f687e994dc523945948c321af25061b39b",
            "postgres_scanner": "39e661c5cfb678c9472f5acd88235f84f3ef4126e7444c7c8a01214d9b291601",
        }
        connection = duckdb.connect(config={"memory_limit": "512MB", "threads": "2"})
        try:
            for name, digest in expected.items():
                path = self.extensions / (name + ".duckdb_extension")
                if not path.is_absolute() or path.resolve(strict=True) != path:
                    raise CatalogError()
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise CatalogError()
                connection.execute("LOAD " + _literal(str(path)))
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def _postgres(self, credential: dict[str, str]) -> Iterator[DuckDBPyConnection]:
        connection = self._connect()
        try:
            connection.execute("ATTACH " + _literal(_dsn(credential)) + " AS pg (TYPE POSTGRES)")
            yield connection
        finally:
            connection.close()

    def _query(self, connection: DuckDBPyConnection, sql: str) -> list[Any]:
        return connection.execute("SELECT * FROM postgres_query('pg', ?)", [sql]).fetchall()

    def _execute(self, connection: DuckDBPyConnection, sql: str) -> None:
        # Exact retained DuckLake417a9117f5 PostgresMetadataManager::Execute API.
        connection.execute("CALL postgres_execute('pg', " + _literal(sql) + ", prepare=FALSE)")

    def _namespace(self, connection: DuckDBPyConnection, binding: CatalogBinding) -> list[Any]:
        return self._query(
            connection,
            "SELECT oid::bigint,pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname="
            + _literal(binding.metadata_schema),
        )

    def _evidence(
        self, connection: DuckDBPyConnection, binding: CatalogBinding, credential: dict[str, str]
    ) -> dict[str, Any]:
        namespace = self._namespace(connection, binding)
        if len(namespace) != 1 or namespace[0][1:] != (
            credential["user"],
            "dango:" + binding.binding_id,
        ):
            raise CatalogError()
        relations = self._query(
            connection,
            "SELECT oid::bigint,relname,relkind::text FROM pg_class WHERE relnamespace="
            + str(namespace[0][0])
            + " ORDER BY relname",
        )
        return {
            "namespace": [list(row) for row in namespace],
            "relations": [list(row) for row in relations],
            "dependents": self._query(connection, _dependencies(relations))[0][0],
        }

    def prepare(
        self,
        binding: CatalogBinding,
        credential: dict[str, str],
        checkpoint: Callable[..., None],
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Persist namespace identity before the single writable catalog bootstrap."""
        with self._postgres(credential) as connection:
            if evidence is None:
                schema = _quote(binding.metadata_schema)
                self._execute(
                    connection,
                    "DO $dango$ BEGIN CREATE SCHEMA "
                    + schema
                    + "; COMMENT ON SCHEMA "
                    + schema
                    + " IS "
                    + _literal("dango:" + binding.binding_id)
                    + "; END $dango$",
                )
                evidence = self._evidence(connection, binding, credential)
                checkpoint(evidence, "namespace")
            elif (
                self._evidence(connection, binding, credential) != evidence or evidence["relations"]
            ):
                raise CatalogError()
        with self.attach(binding, credential, read_only=False):
            pass
        checkpoint(self.inspect_owned(binding, credential, None), "prepared")

    @contextmanager
    def attach(
        self, binding: CatalogBinding, credential: dict[str, str], *, read_only: bool
    ) -> Iterator[DuckDBPyConnection]:
        """Reopen using fixed metadata version/options and explicit read-only attachment."""
        connection = self._connect()
        try:
            options = (
                "METADATA_SCHEMA "
                + _literal(binding.metadata_schema)
                + ", DATA_PATH "
                + _literal(binding.data_root_ref)
            )
            options += (
                ", DUCKLAKE_VERSION '1.1', AUTOMATIC_MIGRATION FALSE, DATA_INLINING_ROW_LIMIT 0"
            )
            if read_only:
                options += ", READ_ONLY"
            connection.execute(
                "ATTACH "
                + _literal("ducklake:postgres:" + _dsn(credential))
                + " AS catalog ("
                + options
                + ")"
            )
            yield connection
        finally:
            connection.close()

    def inspect_owned(
        self, binding: CatalogBinding, credential: dict[str, str], evidence: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Compare immutable native identities and fixed version/data-root bindings."""
        with self._postgres(credential) as connection:
            current = self._evidence(connection, binding, credential)
            if evidence is not None and current != evidence:
                raise CatalogError()
            metadata = dict(
                self._query(
                    connection,
                    "SELECT key,value FROM "
                    + _quote(binding.metadata_schema)
                    + ".ducklake_metadata WHERE scope IS NULL AND key IN ('version','data_path')",
                )
            )
            if metadata.get("version") != "1.1" or Path(metadata.get("data_path", "")) != Path(
                binding.data_root_ref
            ):
                raise CatalogError()
            self._files(connection, binding)
            return current

    def _files(self, connection: DuckDBPyConnection, binding: CatalogBinding) -> set[str]:
        root, files = Path(binding.data_root_ref), set()
        for table in ("ducklake_data_file", "ducklake_delete_file"):
            rows = self._query(
                connection,
                "SELECT s.path,s.path_is_relative,t.path,t.path_is_relative,f.path,f.path_is_relative,f.end_snapshot FROM "
                + _quote(binding.metadata_schema)
                + "."
                + table
                + " f JOIN "
                + _quote(binding.metadata_schema)
                + ".ducklake_table t USING(table_id) JOIN "
                + _quote(binding.metadata_schema)
                + ".ducklake_schema s USING(schema_id)",
            )
            files.update(catalog_file_paths(rows, root))
        return files

    def remove_owned(
        self,
        binding: CatalogBinding,
        credential: dict[str, str],
        evidence: dict[str, Any],
        intent: Callable[[set[str]], None],
        *,
        resuming: bool,
    ) -> None:
        """Atomically drop exact owned tables and RESTRICT schema removal; never CASCADE.

        Metadata table locks fence ordinary DuckLake writers. A changed snapshot/OID or
        any foreign schema dependency aborts the entire DO statement, preserving tables.
        """
        with self._postgres(credential) as connection:
            namespace = self._namespace(connection, binding)
            if not namespace and resuming:
                return  # Durable physical removal intent already binds all remaining files.
            if self._evidence(connection, binding, credential) != evidence:
                raise CatalogError()
            schema = _quote(binding.metadata_schema)
            snapshots = (
                "SELECT coalesce(string_agg(snapshot_id::text,',' ORDER BY snapshot_id),'') FROM "
                + schema
                + ".ducklake_snapshot"
            )
            watermark = self._query(connection, snapshots)[0][0]
            intent(self._files(connection, binding))
            sql = restrict_drop_sql(
                binding.metadata_schema,
                binding.binding_id,
                binding.data_root_ref,
                snapshots,
                watermark,
                evidence,
                _dependencies(evidence["relations"]),
            )
            self._execute(connection, sql)


class CatalogAllocator:
    """Reauthorize current persisted bindings before credentials or native operations."""

    def __init__(
        self,
        state_root: Path,
        data_root: Path,
        *,
        port: NativeCatalogPort,
        credential_ref: SecretRef,
        catalog_database: str,
        bundle_id: str,
        resolve: Callable[[SecretRef], dict[str, str] | None],
        authorize: Callable[[OperationContext, CatalogBinding, Action], bool],
    ):
        self.state_root, self.data_root = state_root, data_root
        self.port, self.ref, self.database, self.bundle = (
            port,
            credential_ref,
            catalog_database,
            bundle_id,
        )
        self.resolve, self.authorize = resolve, authorize

    @contextmanager
    def _scope(
        self, context: OperationContext, workspace: str, binding: CatalogBinding | None = None
    ) -> Iterator[OwnedWorkspace]:
        if type(context) is not OperationContext or type(workspace) is not str:
            raise CatalogError()
        OperationContext.model_validate(context.model_dump())
        opaque_identifier(workspace)
        with OwnedWorkspace(
            self.state_root, self.data_root, context.project_id, workspace
        ).locked() as owned:
            if binding is not None:
                if type(binding) is not CatalogBinding or owned.record is None:
                    raise CatalogError()
                current = CatalogBinding.model_validate(owned.record["binding"])
                if current != CatalogBinding.model_validate(binding.model_dump()):
                    raise CatalogError()
            yield owned

    def _access(
        self, context: OperationContext, binding: CatalogBinding, action: Action
    ) -> dict[str, str]:
        if context.project_id != binding.project_id or context.actor_id != binding.owner_actor_id:
            raise CatalogError()
        if binding.catalog_database != self.database or binding.bundle_id != self.bundle:
            raise CatalogError()
        ref = SecretRef.model_validate(binding.credential_ref.model_dump())
        if ref.project_id != binding.project_id or ref.actor_id != binding.owner_actor_id:
            raise CatalogError()
        if self.authorize(context, binding, action) is not True:
            raise CatalogError()
        return _credential(self.resolve(ref), binding.catalog_database)

    def allocate(self, context: OperationContext, workspace_id: str) -> CatalogBinding:
        """Commit intent before creation; return existing records for authorized recovery."""
        try:
            with self._scope(context, workspace_id) as owned:
                if owned.record is not None:
                    binding = CatalogBinding.model_validate(owned.record["binding"])
                    self._access(context, binding, "allocate")
                    if owned.record["phase"] == "removed":
                        raise CatalogError()
                    return binding
                token = uuid4().hex
                binding = CatalogBinding(
                    binding_id=token,
                    project_id=context.project_id,
                    workspace_id=workspace_id,
                    owner_actor_id=context.actor_id,
                    catalog_database=self.database,
                    metadata_schema="dango_" + token,
                    data_root_ref=str(self.data_root / token),
                    credential_ref=self.ref,
                    bundle_id=self.bundle,
                    state="allocating",
                    revision=0,
                )
                credential = self._access(context, binding, "allocate")
                try:
                    owned.begin(binding.model_dump(mode="json"))
                    self.port.prepare(binding, credential, owned.checkpoint)
                    return CatalogBinding.model_validate(owned.ready())
                except BaseException:
                    owned.recover()
                    raise
        except Exception:
            raise CatalogError() from None

    @contextmanager
    def attach(
        self, context: OperationContext, binding: CatalogBinding, *, read_only: bool
    ) -> Iterator[DuckDBPyConnection]:
        """Authorize the exact current manifest before native inspection and attach."""
        try:
            with self._scope(context, binding.workspace_id, binding) as owned:
                if (
                    type(read_only) is not bool
                    or binding.state != "ready"
                    or owned.record is None
                    or owned.record["phase"] != "prepared"
                ):
                    raise CatalogError()
                credential = self._access(context, binding, "read" if read_only else "write")
                owned.validate_root()
                with owned.root():
                    self.port.inspect_owned(binding, credential, owned.record["native"])
                    with self.port.attach(binding, credential, read_only=read_only) as connection:
                        yield connection
        except Exception:
            raise CatalogError() from None

    def reconcile(self, context: OperationContext, binding: CatalogBinding) -> CatalogBinding:
        """Resume only recorded complete/empty-owned phases; refuse ambiguous objects."""
        try:
            with self._scope(context, binding.workspace_id, binding) as owned:
                credential = self._access(context, binding, "reconcile")
                if owned.record is None or owned.record["root"] is None:
                    raise CatalogError()
                phase = owned.record["phase"]
                owned.validate_root()
                with owned.root():
                    if phase in {"root", "namespace"}:
                        self.port.prepare(
                            binding, credential, owned.checkpoint, owned.record["native"]
                        )
                    elif phase == "prepared":
                        self.port.inspect_owned(binding, credential, owned.record["native"])
                    else:
                        raise CatalogError()
                return (
                    binding
                    if binding.state == "ready"
                    else CatalogBinding.model_validate(owned.ready())
                )
        except Exception:
            raise CatalogError() from None

    def remove_owned(self, context: OperationContext, binding: CatalogBinding) -> None:
        """Journal removal before any drop and resume only exact recorded physical effects."""
        try:
            with self._scope(context, binding.workspace_id, binding) as owned:
                credential = self._access(context, binding, "remove")
                if owned.record is None or owned.record["native"] is None:
                    raise CatalogError()
                phase = owned.record["phase"]
                if phase == "removed":
                    return
                if phase in {"prepared", "removing", "native_removed"}:
                    self.port.remove_owned(
                        binding,
                        credential,
                        owned.record["native"],
                        owned.removal_intent,
                        resuming=phase != "prepared",
                    )
                    owned.native_removed()
                else:
                    raise CatalogError()
                owned.finish_removal()
        except Exception:
            raise CatalogError() from None
