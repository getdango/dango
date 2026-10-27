"""dango/auth/native_permissions.py

Validate local native identity records and plan rights from explicit server-owned grants.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from typing_extensions import Self

from dango.auth.models import Role, User
from dango.auth.permissions import has_permission
from dango.operations.contracts import OperationContext

__all__ = [
    "NativeAccessPlan",
    "NativeGrantAuthority",
    "NativeGrantRecord",
    "NativeIdentityBinding",
    "NativePolicyError",
    "plan_native_access",
]

_Capability = Literal[
    "dashboard.consume", "metric.explore", "content.write", "definition.write", "project.admin"
]
_ProjectRole = Literal["viewer", "interactive_viewer", "editor", "admin"]
_State = Literal["active", "revoked", "pending"]
_SPACE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def _identifier(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char) in ("Cc", "Cs") for char in value):
        raise ValueError("Identifiers must be nonblank and contain no control characters.")
    return value


_Identifier = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_identifier)]


def _timestamp(value: str) -> str:
    if not _TIMESTAMP.fullmatch(value):
        raise ValueError("Timestamps must be ISO 8601 strings with a timezone offset.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError("Timestamps must be valid timezone-aware dates.") from None
    return value


_Timestamp = Annotated[str, AfterValidator(_timestamp)]


def _spaces(value: dict[str, str]) -> dict[str, str]:
    if any(right not in _SPACE_RANK for right in value.values()):
        raise ValueError("Space rights must be viewer, editor or admin.")
    return value


_SpaceGrants = Annotated[dict[_Identifier, str], AfterValidator(_spaces)]
_IDENTIFIER_ADAPTER = TypeAdapter(_Identifier, config=ConfigDict(strict=True))
_SPACES_ADAPTER = TypeAdapter(tuple[_Identifier, ...], config=ConfigDict(strict=True))


def _revocation(state: _State, revoked_at: str | None) -> None:
    if (state == "revoked") != (revoked_at is not None):
        raise ValueError("Only revoked records require and permit a revocation timestamp.")


class _NativeModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class NativeAccessPlan(_NativeModel):
    """Describe minimal intended rights; construction never provisions or authenticates."""

    actor_id: _Identifier
    project_id: _Identifier
    permission_revision: _Identifier
    org_role: Literal["member"]
    project_role: _ProjectRole
    space_grants: _SpaceGrants
    definition_write: bool

    @model_validator(mode="after")
    def _write_requires_builder(self) -> Self:
        if self.definition_write and self.project_role not in ("editor", "admin"):
            raise ValueError("Definition write requires explicit builder intent.")
        return self


class NativeIdentityBinding(_NativeModel):
    """Carry identity metadata and secret references, never secret values or access authority."""

    binding_id: _Identifier
    actor_id: _Identifier
    project_id: _Identifier
    native_user_id: _Identifier | None
    native_project_id: _Identifier | None
    project_rights: tuple[_Identifier, ...]
    space_grants: _SpaceGrants
    credential_secret_ref: _Identifier | None
    credential_version: _Identifier | None
    credential_expiry: _Timestamp | None
    permission_revision: _Identifier
    revoked_at: _Timestamp | None
    state: _State

    @model_validator(mode="after")
    def _consistent_state(self) -> Self:
        _revocation(self.state, self.revoked_at)
        if self.state == "active" and any(
            value is None
            for value in (
                self.native_user_id,
                self.native_project_id,
                self.credential_secret_ref,
                self.credential_version,
            )
        ):
            raise ValueError(
                "Active bindings require native IDs and credential references/version."
            )
        return self


class NativeGrantRecord(_NativeModel):
    """Snapshot current explicit grants from the trusted server authority, without credentials."""

    actor_id: _Identifier
    project_id: _Identifier
    permission_revision: _Identifier
    project_rights: tuple[_Capability, ...]
    space_grants: _SpaceGrants
    state: _State
    revoked_at: _Timestamp | None

    @model_validator(mode="after")
    def _consistent_state(self) -> Self:
        _revocation(self.state, self.revoked_at)
        return self


class NativeGrantAuthority(Protocol):
    """Trusted server integration, never supplied by request payloads.

    Resolve the current record and current permission revision, including revocations.
    Never resolve historical grants selected by a caller's expected revision. Persistence,
    atomic revocation and native enforcement belong to the later server integration.
    """

    def resolve_grant(self, *, actor_id: str, project_id: str) -> NativeGrantRecord | None:
        """Return the current explicit grant snapshot, or None when no grant exists."""
        ...


class NativePolicyError(ValueError):
    """Expose only a fixed safe denial code and action at the planning boundary."""

    def __init__(self) -> None:
        self.code = "native_access_denied"
        self.action = "Ask a project administrator to verify current project and space grants."
        super().__init__(f"{self.code}: {self.action}")


# Each row requires its exact explicit project right. No project-right hierarchy.
# These are normalized local intents, not native API enum spellings.
_POLICY: dict[str, tuple[str, _ProjectRole, str]] = {
    "dashboard.consume": ("dashboard.view", "viewer", "viewer"),
    "metric.explore": ("dashboard.view", "interactive_viewer", "viewer"),
    "content.write": ("dashboard.create", "editor", "editor"),
    "definition.write": ("dbt.manage", "editor", "editor"),
    "project.admin": ("dashboard.manage", "admin", "admin"),
}


def plan_native_access(
    context: OperationContext,
    user: User,
    project_id: str,
    space_ids: tuple[str, ...],
    *,
    grant_authority: NativeGrantAuthority,
    definition_write: bool = False,
) -> NativeAccessPlan:
    """Plan least required rights from server identity, existing permissions and current grants.

    The server must inject the authority independently of request input. This local
    policy performs no persistence or native IO; its output is not a reusable access
    credential. Reauthorize at actual use, including history, results and export.
    """
    try:
        return _plan(context, user, project_id, space_ids, grant_authority, definition_write)
    except Exception:
        # Never render payloads, validation inputs or authority exceptions publicly.
        raise NativePolicyError() from None


def _plan(
    context: OperationContext,
    user: User,
    project_id: str,
    space_ids: tuple[str, ...],
    grant_authority: NativeGrantAuthority,
    definition_write: bool,
) -> NativeAccessPlan:
    if type(context) is not OperationContext or type(user) is not User:
        raise NativePolicyError()
    # Do not trust model_copy/model_construct or shallow mutable nested fields.
    context = OperationContext.model_validate(context.model_dump(warnings=False))
    project_id = _IDENTIFIER_ADAPTER.validate_python(project_id)
    space_ids = _SPACES_ADAPTER.validate_python(space_ids)
    if (
        type(definition_write) is not bool
        or type(user.is_active) is not bool
        or not user.is_active
        or not isinstance(user.role, Role)
        or type(user.id) is not str
        or user.id != context.actor_id
        or project_id != context.project_id
        or len(set(space_ids)) != len(space_ids)
    ):
        raise NativePolicyError()
    policy = _POLICY.get(context.capability)
    if policy is None or definition_write != (context.capability == "definition.write"):
        raise NativePolicyError()
    permission, project_role, space_role = policy
    if not has_permission(user, permission):
        raise NativePolicyError()
    grant = grant_authority.resolve_grant(actor_id=context.actor_id, project_id=project_id)
    if type(grant) is not NativeGrantRecord:
        raise NativePolicyError()
    grant = NativeGrantRecord.model_validate(grant.model_dump(warnings=False))
    if (
        grant.actor_id != context.actor_id
        or grant.project_id != project_id
        or grant.permission_revision != context.permission_revision
        or grant.state != "active"
        or grant.revoked_at is not None
        or context.capability not in grant.project_rights
    ):
        raise NativePolicyError()
    for space_id in space_ids:
        right = grant.space_grants.get(space_id)
        if right is None or _SPACE_RANK[right] < _SPACE_RANK[space_role]:
            raise NativePolicyError()
    return NativeAccessPlan(
        actor_id=context.actor_id,
        project_id=project_id,
        permission_revision=grant.permission_revision,
        org_role="member",
        project_role=project_role,
        space_grants=dict.fromkeys(space_ids, space_role),
        definition_write=definition_write,
    )
