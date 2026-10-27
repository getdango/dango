"""tests/unit/test_native_permissions.py

Verify explicit native grant policy with independent rights and denial fixtures.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from dango.auth.models import Role, User
from dango.auth.native_permissions import (
    NativeAccessPlan,
    NativeGrantRecord,
    NativeIdentityBinding,
    NativePolicyError,
    plan_native_access,
)
from dango.operations.contracts import OperationContext

pytestmark = pytest.mark.unit

_CONTEXT = {
    "operation_id": "op-1",
    "project_id": "project-1",
    "actor_id": "actor-1",
    "capability": "dashboard.consume",
    "permission_revision": "revision-2",
    "trace_id": "trace-1",
}
_GRANT = {
    "actor_id": "actor-1",
    "project_id": "project-1",
    "permission_revision": "revision-2",
    "project_rights": ("dashboard.consume",),
    "space_grants": {"shared": "viewer"},
    "state": "active",
    "revoked_at": None,
}
_PLAN = {
    "actor_id": "actor-1",
    "project_id": "project-1",
    "permission_revision": "revision-2",
    "org_role": "member",
    "project_role": "viewer",
    "space_grants": {"shared": "viewer"},
    "definition_write": False,
}
_BINDING = {
    "binding_id": "binding-1",
    "actor_id": "actor-1",
    "project_id": "project-1",
    "native_user_id": "native-user",
    "native_project_id": "native-project",
    "project_rights": ("dashboard.consume",),
    "space_grants": {"shared": "viewer"},
    "credential_secret_ref": "secret-ref-1",
    "credential_version": "version-1",
    "credential_expiry": "2030-01-01T00:00:00Z",
    "permission_revision": "revision-2",
    "revoked_at": None,
    "state": "active",
}
_CASES = (
    ("dashboard.consume", "viewer", "viewer", False, (Role.VIEWER, Role.EDITOR, Role.ADMIN)),
    (
        "metric.explore",
        "interactive_viewer",
        "viewer",
        False,
        (Role.VIEWER, Role.EDITOR, Role.ADMIN),
    ),
    ("content.write", "editor", "editor", False, (Role.EDITOR, Role.ADMIN)),
    ("definition.write", "editor", "editor", True, (Role.EDITOR, Role.ADMIN)),
    ("project.admin", "admin", "admin", False, (Role.ADMIN,)),
)


class Authority:
    def __init__(self, record):
        self.record = record
        self.calls = []

    def resolve_grant(self, *, actor_id, project_id):
        self.calls.append((actor_id, project_id))
        return self.record


def _grant(**changes):
    return NativeGrantRecord(**{**_GRANT, **changes})


def _plan(*, capability="dashboard.consume", role=Role.VIEWER, record=None, **changes):
    values = {
        "context": OperationContext(**{**_CONTEXT, "capability": capability}),
        "user": User(id="actor-1", email="a@example.test", role=role),
        "project_id": "project-1",
        "space_ids": ("shared",),
        "grant_authority": Authority(record if record is not None else _grant()),
    }
    return plan_native_access(**{**values, **changes})


def _denied(**changes):
    with pytest.raises(NativePolicyError) as error:
        _plan(**changes)
    assert error.value.code == "native_access_denied"
    assert (
        error.value.action
        == "Ask a project administrator to verify current project and space grants."
    )
    return error.value


def test_consumer_gets_only_explicit_project_and_space_rights():
    authority = Authority(_grant(space_grants={"shared": "admin", "private": "editor"}))
    actual = _plan(grant_authority=authority)
    assert actual.model_dump() == _PLAN
    assert authority.calls == [("actor-1", "project-1")]
    _denied(space_ids=("shared", "private"))
    _denied(record=_grant(project_rights=()))


def test_explorer_not_definition_editor():
    actual = _plan(capability="metric.explore", record=_grant(project_rights=("metric.explore",)))
    assert actual.model_dump() == {**_PLAN, "project_role": "interactive_viewer"}
    _denied(capability="metric.explore")
    _denied(
        capability="metric.explore",
        definition_write=True,
        record=_grant(project_rights=("metric.explore", "definition.write")),
    )


def test_forged_payload_role_cannot_elevate():
    with pytest.raises(ValidationError):
        OperationContext(**_CONTEXT, role="admin")
    for role in Role:
        _denied(role=role, grant_authority=Authority(None))
        _denied(role=role, record=_grant(project_rights=("project.admin",)))
    forged = _grant(actor_id="actor-2", project_rights=("project.admin",))
    _denied(role=Role.ADMIN, capability="project.admin", record=forged)


@pytest.mark.parametrize(
    "changes",
    [
        {"permission_revision": "revision-1"},
        {"state": "pending"},
        {"state": "revoked", "revoked_at": "2026-10-28T01:00:00+00:00"},
        {"actor_id": "actor-2"},
        {"project_id": "project-2"},
    ],
)
def test_revoked_or_stale_binding_refused(changes):
    _denied(record=_grant(**changes))


def test_private_preview_not_inherited_from_org():
    for role in Role:
        _denied(role=role, project_id="preview", space_ids=("preview-space",))
        _denied(role=role, space_ids=("private",))
    preview = _grant(project_id="preview", space_grants={"preview-space": "viewer"})
    context = OperationContext(**{**_CONTEXT, "project_id": "preview"})
    assert _plan(
        context=context, project_id="preview", space_ids=("preview-space",), record=preview
    ).space_grants == {"preview-space": "viewer"}


@pytest.mark.parametrize("capability,project_role,space_role,write,roles", _CASES)
@pytest.mark.parametrize("role", tuple(Role))
def test_capability_role_matrix(capability, project_role, space_role, write, roles, role):
    arguments = {
        "capability": capability,
        "role": role,
        "definition_write": write,
        "record": _grant(project_rights=(capability,), space_grants={"shared": "admin"}),
    }
    if role not in roles:
        _denied(**arguments)
        return
    actual = _plan(**arguments)
    assert actual.model_dump() == {
        **_PLAN,
        "project_role": project_role,
        "space_grants": {"shared": space_role},
        "definition_write": write,
    }


@pytest.mark.parametrize("capability,project_role,space_role,write,roles", _CASES)
def test_exact_project_right_and_sufficient_space_are_required(
    capability,
    project_role,
    space_role,
    write,
    roles,
):
    _denied(
        capability=capability,
        role=Role.ADMIN,
        definition_write=write,
        record=_grant(project_rights=()),
    )
    _denied(
        capability=capability,
        role=Role.ADMIN,
        definition_write=write,
        record=_grant(project_rights=(capability,), space_grants={}),
    )
    if space_role != "viewer":
        _denied(
            capability=capability,
            role=Role.ADMIN,
            definition_write=write,
            record=_grant(project_rights=(capability,), space_grants={"shared": "viewer"}),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"grant_authority": None},
        {"grant_authority": {}},
        {"grant_authority": Authority(None)},
        {"grant_authority": Authority(_GRANT)},
        {"context": None},
        {"user": None},
        {"project_id": None},
        {"project_id": ""},
        {"project_id": "project-2"},
        {"space_ids": ["shared"]},
        {"space_ids": ("",)},
        {"space_ids": ("shared", "shared")},
        {"space_ids": (1,)},
        {"space_ids": None},
        {"definition_write": 1},
        {"capability": "unknown"},
    ],
)
def test_missing_invalid_and_unknown_boundary_inputs_deny(changes):
    _denied(**changes)


def test_active_matching_user_is_required():
    for changes in ({"id": "actor-2"}, {"is_active": False}, {"is_active": 1}, {"role": "unknown"}):
        user = User(id="actor-1", email="a@example.test").model_copy(update=changes)
        _denied(user=user)
    context = OperationContext(**_CONTEXT).model_copy(update={"schema_version": True})
    _denied(context=context)


def test_definition_boolean_alone_never_grants_write():
    _denied(role=Role.ADMIN, definition_write=True)
    record = _grant(project_rights=("definition.write",), space_grants={"shared": "editor"})
    _denied(capability="definition.write", role=Role.ADMIN, record=record)
    assert (
        _plan(
            capability="definition.write", role=Role.ADMIN, record=record, definition_write=True
        ).definition_write
        is True
    )


def test_empty_requested_spaces_grant_no_content_scope():
    assert _plan(space_ids=()).model_dump() == {**_PLAN, "space_grants": {}}
    _denied(space_ids=(), record=_grant(project_rights=()))


def test_mutated_nested_authority_and_copied_records_are_revalidated():
    record = _grant()
    record.space_grants["shared"] = "owner"
    _denied(record=record)
    _denied(record=_grant().model_copy(update={"project_rights": ("unknown",)}))
    _denied(
        record=_grant().model_copy(update={"state": "active", "revoked_at": "2026-10-28T00:00:00Z"})
    )
    record = _grant(space_grants={"shared": "admin"})
    actual = _plan(record=record)
    record.space_grants["shared"] = "viewer"
    actual.space_grants["shared"] = "admin"
    assert record.space_grants == {"shared": "viewer"}
    assert _plan(record=record).space_grants == {"shared": "viewer"}


@pytest.mark.parametrize(
    "model,values",
    [
        (NativeAccessPlan, _PLAN),
        (NativeIdentityBinding, _BINDING),
        (NativeGrantRecord, _GRANT),
    ],
)
def test_models_have_exact_required_fields_and_are_strict_frozen_extra_forbid(model, values):
    record = model(**values)
    assert set(model.model_fields) == set(values)
    assert all(field.is_required() for field in model.model_fields.values())
    for field in values:
        with pytest.raises(ValidationError):
            model(**{key: value for key, value in values.items() if key != field})
        with pytest.raises(ValidationError):
            setattr(record, field, values[field])
    with pytest.raises(ValidationError):
        model(**values, unknown="CANARY_PRIVATE_8321")
    for field in ("actor_id", "project_id", "permission_revision"):
        for invalid in (None, "", "  ", "a\n", True, 1, float("nan"), float("inf")):
            with pytest.raises(ValidationError):
                model(**{**values, field: invalid})
    assert record.model_config["strict"] is True
    assert record.model_config["frozen"] is True
    assert record.model_config["extra"] == "forbid"


@pytest.mark.parametrize(
    "field",
    [
        "binding_id",
        "native_user_id",
        "native_project_id",
        "credential_secret_ref",
        "credential_version",
    ],
)
def test_active_binding_requires_nonempty_native_identity_and_secret_references(field):
    for invalid in (None, "", " ", 1):
        with pytest.raises(ValidationError):
            NativeIdentityBinding(**{**_BINDING, field: invalid})


def test_binding_pending_and_revoked_states_are_consistent():
    pending = {
        **_BINDING,
        "state": "pending",
        "native_user_id": None,
        "native_project_id": None,
        "credential_secret_ref": None,
        "credential_version": None,
        "credential_expiry": None,
    }
    assert NativeIdentityBinding(**pending).state == "pending"
    revoked = {**pending, "state": "revoked", "revoked_at": "2026-10-28T01:00:00Z"}
    assert NativeIdentityBinding(**revoked).revoked_at == "2026-10-28T01:00:00Z"
    for values in (_BINDING, _GRANT):
        model = NativeIdentityBinding if "binding_id" in values else NativeGrantRecord
        for changes in (
            {"state": "revoked"},
            {"state": "unknown"},
            {"revoked_at": "2026-10-28T01:00:00Z"},
            {"state": "pending", "revoked_at": "2026-10-28T01:00:00Z"},
        ):
            with pytest.raises(ValidationError):
                model(**{**values, **changes})


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-28",
        "2026-10-28T01:00:00",
        "nonsense",
        "",
        "2026-02-30T01:00:00Z",
        "2026-10-28T01:00:00+00:60",
        1,
        True,
    ],
)
def test_timestamp_strings_require_valid_timezone_aware_values(value):
    with pytest.raises(ValidationError):
        NativeIdentityBinding(**{**_BINDING, "credential_expiry": value})
    with pytest.raises(ValidationError):
        _grant(state="revoked", revoked_at=value)


@pytest.mark.parametrize(
    "value",
    ["2030-01-01T00:00:00Z", "2030-01-01T08:00:00+08:00", "2030-01-01T00:00:00.123456-01:00", None],
)
def test_binding_expiry_allows_offset_or_explicit_null(value):
    assert (
        NativeIdentityBinding(**{**_BINDING, "credential_expiry": value}).credential_expiry == value
    )


@pytest.mark.parametrize(
    "model,values,changes",
    [
        (NativeGrantRecord, _GRANT, {"project_rights": ["dashboard.consume"]}),
        (NativeGrantRecord, _GRANT, {"project_rights": ("unknown",)}),
        (NativeGrantRecord, _GRANT, {"space_grants": {"shared": "unknown"}}),
        (NativeGrantRecord, _GRANT, {"space_grants": {"": "viewer"}}),
        (NativeIdentityBinding, _BINDING, {"project_rights": ("",)}),
        (NativeIdentityBinding, _BINDING, {"space_grants": {"shared": None}}),
        (NativeAccessPlan, _PLAN, {"org_role": "admin"}),
        (NativeAccessPlan, _PLAN, {"project_role": "owner"}),
        (NativeAccessPlan, _PLAN, {"definition_write": 1}),
        (NativeAccessPlan, _PLAN, {"definition_write": True}),
    ],
)
def test_rights_and_normalized_roles_reject_invalid_shapes(model, values, changes):
    with pytest.raises(ValidationError):
        model(**{**values, **changes})


def test_errors_and_logs_do_not_expose_authority_payload_or_exception(caplog, recwarn):
    secret = "CANARY_PRIVATE_8321"

    class BrokenAuthority:
        def resolve_grant(self, **kwargs):
            raise RuntimeError(secret)

    error = _denied(grant_authority=BrokenAuthority())
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert secret not in str(error) + repr(error) + caplog.text
    error = _denied(grant_authority=Authority({"token": secret}))
    assert secret not in str(error)
    malformed = _grant().model_copy(update={"space_grants": {"shared": {"token": secret}}})
    error = _denied(record=malformed)
    assert secret not in str(error) + " ".join(str(item.message) for item in recwarn)
    for model, values in (
        (NativeAccessPlan, _PLAN),
        (NativeIdentityBinding, _BINDING),
        (NativeGrantRecord, _GRANT),
    ):
        with pytest.raises(ValidationError) as failure:
            model(**values, token=secret)
        assert secret not in str(failure.value)


def test_fresh_import_performs_no_native_network_credential_or_file_initialization(tmp_path):
    script = """
import os
import sys
import dango.auth.models
import dango.auth.permissions

def guard(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.mkdir'):
        raise AssertionError('Unexpected side effect')
    if event == 'open':
        mode, flags = args[1], args[2]
        if (mode and any(char in mode for char in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError('Unexpected file write')
    if event == 'import' and args[0].split('.')[0] in ('keyring', 'duckdb', 'dlt', 'dbt'):
        raise AssertionError('Unexpected credential or native import')

sys.addaudithook(guard)
import dango.auth.native_permissions
print('import-safe')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "HOME": str(tmp_path),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-safe"
    assert list(tmp_path.iterdir()) == []
