"""tests/unit/test_operation_contracts.py
Verify strict operation envelopes, safe failures and independently computed request identity.
"""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from dango.operations.contracts import (
    ContractValidationError,
    OperationContext,
    OperationRequest,
    OutcomeReceipt,
    canonical_request_bytes,
    request_digest,
)

pytestmark = pytest.mark.unit

_CONTEXT = {
    "operation_id": "operation-1",
    "project_id": "project-1",
    "actor_id": "actor-1",
    "capability": "query:read",
    "permission_revision": "permission-1",
    "trace_id": "trace-1",
}
_CONTEXT_FIELDS = tuple(_CONTEXT)
_INVALID_TEXT = ("", "  ", "a\n", "a\x00b", "a\x85b", "x" * 257, None, 1, True, b"id")


def _context(**changes):
    return OperationContext(**{**_CONTEXT, **changes})


def _request(**changes):
    return OperationRequest(
        **{"operation_name": "query", "payload": {"b": 2, "a": [1, None]}, **changes}
    )


@pytest.mark.parametrize("field", _CONTEXT_FIELDS)
def test_context_rejects_missing_empty_and_coerced_fields(field):
    missing = {key: value for key, value in _CONTEXT.items() if key != field}
    with pytest.raises(ValidationError):
        OperationContext(**missing)
    for value in _INVALID_TEXT:
        with pytest.raises(ValidationError):
            _context(**{field: value})
    preserved = " actor " + "x" * 249
    assert len(preserved) == 256
    assert getattr(_context(**{field: preserved}), field) == preserved


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 0, 2, None])
@pytest.mark.parametrize("model", [OperationContext, OperationRequest, OutcomeReceipt])
def test_schema_version_rejects_bool_string_and_other_versions(model, version):
    values = {
        OperationContext: _CONTEXT,
        OperationRequest: {"operation_name": "query", "payload": {}},
        OutcomeReceipt: {"operation_id": "operation-1", "status": "success"},
    }[model]
    with pytest.raises(ValidationError):
        model(**values, schema_version=version)
    record = model(**values, schema_version=1)
    assert type(record.schema_version) is int
    assert record.schema_version == 1
    assert record.model_config == {
        "extra": "forbid",
        "frozen": True,
        "strict": True,
        "hide_input_in_errors": True,
    }
    with pytest.raises(ValidationError):
        record.schema_version = 1
    with pytest.raises(ValidationError):
        model(**values, unknown="value")


@pytest.mark.parametrize(
    "field", ["actor", "actor_id", "project_id", "capability", "approval", "is_mutation"]
)
def test_request_rejects_server_identity_fields(field):
    with pytest.raises(ValidationError):
        _request(**{field: "forged"})
    assert _request(payload={"actor": "domain-actor"}).payload == {"actor": "domain-actor"}


@pytest.mark.parametrize("field", ["operation_name", "expected_revision", "idempotency_key"])
def test_request_identifiers_are_strict_bounded_and_preserved(field):
    for value in _INVALID_TEXT:
        if value is None and field != "operation_name":
            continue
        with pytest.raises(ValidationError):
            _request(**{field: value})
    value = " x "
    assert getattr(_request(**{field: value}), field) == value
    assert getattr(_request(**{field: "x" * 256}), field) == "x" * 256
    with pytest.raises(ValidationError):
        OperationRequest(operation_name="query")
    with pytest.raises(ValidationError):
        OperationRequest(payload={})


def test_canonical_request_matches_literal_utf8_json():
    expected = b'{"expected_revision":null,"operation_name":"query","payload":{"a":[1,null],"b":2}}'
    assert canonical_request_bytes(_request()) == expected
    assert canonical_request_bytes(_request(expected_revision=None)) == expected
    assert canonical_request_bytes(_request(payload={"a": [1, None], "b": 2})) == expected
    utf8_expected = (
        b'{"expected_revision":null,"operation_name":"query","payload":{"\xc3\xa9":"\xe9\x9b\xaa"}}'
    )
    assert canonical_request_bytes(_request(payload={"é": "雪"})) == utf8_expected


def test_digest_matches_independent_hashlib_oracle():
    oracle = {
        "schema_version": 1,
        "project_id": "project-1",
        "actor_id": "actor-1",
        "capability": "query:read",
        "permission_revision": "permission-1",
        "request": {
            "operation_name": "query",
            "payload": {"b": 2, "a": [1, None]},
            "expected_revision": None,
        },
    }
    expected_bytes = json.dumps(
        oracle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    expected = hashlib.sha256(expected_bytes).hexdigest()
    actual = request_digest(_context(), _request())
    assert actual == expected
    assert len(actual) == 64 and actual == actual.lower()


def test_trace_and_operation_id_do_not_change_request_identity():
    original = request_digest(_context(), _request())
    assert request_digest(_context(trace_id="retry-trace"), _request()) == original
    assert request_digest(_context(operation_id="retry-operation"), _request()) == original
    assert request_digest(_context(), _request(idempotency_key="retry-key")) == original
    assert request_digest(_context(), _request(expected_revision=None)) == original


@pytest.mark.parametrize("field", ["actor_id", "project_id", "capability", "permission_revision"])
def test_actor_project_permission_input_and_expected_revision_change_digest(field):
    original = request_digest(_context(), _request())
    assert request_digest(_context(**{field: "changed"}), _request()) != original
    for changes in (
        {"payload": {"a": [1, None], "b": 3}},
        {"expected_revision": "revision-2"},
        {"operation_name": "other-query"},
    ):
        assert request_digest(_context(), _request(**changes)) != original


def test_nested_payload_mutation_does_not_reuse_digest():
    request = _request(payload={"a": {"items": [1]}})
    original = request_digest(_context(), request)
    request.payload["a"]["items"].append(2)
    assert request_digest(_context(), request) != original
    assert (
        canonical_request_bytes(request)
        == b'{"expected_revision":null,"operation_name":"query","payload":{"a":{"items":[1,2]}}}'
    )
    request.payload["a"]["items"].append(float("nan"))
    for function in (canonical_request_bytes, lambda value: request_digest(_context(), value)):
        with pytest.raises(ContractValidationError) as error:
            function(request)
        assert error.value.code == "invalid_payload"
        assert error.value.action


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        {1: "value"},
        b"bytes",
        {1},
        object(),
        datetime(2026, 1, 1),
        Decimal("1"),
        (1, 2),
    ],
)
def test_invalid_payload_types_are_safe(value):
    with pytest.raises(ValidationError, match="invalid_payload"):
        _request(payload={"nested": [value]})
    request = _request(payload={})
    request.payload["nested"] = [value]
    with pytest.raises(ContractValidationError) as error:
        canonical_request_bytes(request)
    assert error.value.code == "invalid_payload"
    with pytest.raises(ContractValidationError):
        request_digest(_context(), request)


def test_json_payload_accepts_only_supported_recursive_values():
    request = _request(payload={"values": [None, True, False, "", 0, -1, 1.5, {}, []]})
    assert json.loads(canonical_request_bytes(request))["payload"] == request.payload
    shared = [1]
    assert _request(payload={"a": shared, "b": shared}).payload == {"a": [1], "b": [1]}
    recursive = []
    recursive.append(recursive)
    with pytest.raises(ValidationError, match="invalid_payload"):
        _request(payload={"cycle": recursive})
    for invalid in (None, [], "text", {1: "value"}):
        with pytest.raises(ValidationError, match="invalid_payload"):
            _request(payload=invalid)
    with pytest.raises(ContractValidationError):
        canonical_request_bytes(_request(payload={"invalid_utf8": "\ud800"}))


@pytest.mark.parametrize(
    "status", ["success", "blocked", "failed", "cancelled", "recovery_required"]
)
def test_receipt_requires_consistent_status_and_action(status):
    values = {"operation_id": "operation-1", "status": status}
    if status == "success":
        receipt = OutcomeReceipt(**values)
        assert receipt.evidence_refs == ()
        assert receipt.resource_revision is receipt.result_ref is None
        for errors in (
            {"error_code": "blocked"},
            {"action": "Retry"},
            {"error_code": "blocked", "action": "Retry"},
        ):
            with pytest.raises(ValidationError):
                OutcomeReceipt(**values, **errors)
    else:
        for errors in ({}, {"error_code": "blocked"}, {"action": "Retry"}):
            with pytest.raises(ValidationError):
                OutcomeReceipt(**values, **errors)
        receipt = OutcomeReceipt(**values, error_code="e" * 512, action="a" * 512)
        assert receipt.status == status
        for field in ("error_code", "action"):
            for invalid in ("", " ", "bad\ntext", "x" * 513, 1):
                with pytest.raises(ValidationError):
                    OutcomeReceipt(
                        **values, **{"error_code": "blocked", "action": "Retry", field: invalid}
                    )


@pytest.mark.parametrize(
    "field", ["operation_id", "resource_revision", "result_ref", "evidence_refs"]
)
def test_receipt_identifiers_and_status_are_strict(field):
    for invalid in _INVALID_TEXT:
        if invalid is None and field in ("resource_revision", "result_ref"):
            continue
        with pytest.raises(ValidationError):
            OutcomeReceipt(
                **{
                    "operation_id": "operation-1",
                    "status": "success",
                    field: (invalid,) if field == "evidence_refs" else invalid,
                }
            )
    with pytest.raises(ValidationError):
        OutcomeReceipt(operation_id="operation-1", status="unknown")
    with pytest.raises(ValidationError):
        OutcomeReceipt(operation_id="operation-1", status="success", evidence_refs=["ref"])
    with pytest.raises(ValidationError):
        OutcomeReceipt(operation_id="operation-1")


def test_validation_error_does_not_echo_canary_secret():
    secret = "CANARY_PRIVATE_TOKEN_73924"
    for factory in (
        lambda: _context(actor_id=secret + "\n"),
        lambda: _request(payload={"token": secret, "invalid": object()}),
        lambda: _request(payload={"token": secret, "invalid": {secret.encode()}}),
        lambda: OutcomeReceipt(operation_id=secret, status="failed"),
    ):
        with pytest.raises(ValidationError) as error:
            factory()
        assert secret not in str(error.value)
    request = _request(payload={"token": secret})
    request.payload["invalid"] = secret.encode()
    with pytest.raises(ContractValidationError) as error:
        canonical_request_bytes(request)
    assert secret not in str(error.value)
    assert secret not in error.value.code + error.value.action


def test_import_has_no_file_network_or_native_initialization(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = """
import os
import sys

forbidden = ("fastapi", "duckdb", "dlt", "dbt", "dango.cli", "dango.web", "dango.auth", "dango.platform")

def guard(event, args):
    if event == "import" and any(args[0] == name or args[0].startswith(name + ".") for name in forbidden):
        raise AssertionError("Unexpected service/native import")
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.mkdir", "os.remove", "os.rename", "os.rmdir"):
        raise AssertionError("Unexpected network/process/filesystem initialization")
    if event == "open":
        mode, flags = args[1], args[2]
        if (mode and any(char in mode for char in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("Unexpected file write")

sys.addaudithook(guard)
import dango.operations
import dango.operations.contracts
assert not any(name in sys.modules for name in forbidden)
print("import-safe")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONPATH": str(root), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-safe"
    assert list(tmp_path.iterdir()) == []
