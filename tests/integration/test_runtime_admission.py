"""tests/integration/test_runtime_admission.py

Verify reviewed native development exports offline; never admit a product runtime.
The explicit live entrypoint requires a separately authorized, already running owned profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dango.platform.bundle import (
    BundleComponent,
    BundleProfile,
    BundleValidationError,
    load_bundle_manifest,
    require_qualified_profile,
)

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parents[1] / "fixtures/runtime_profiles.json"
PROFILE_ID = "t44a-python314-duckdb2-linux-amd64-development"
FIXTURE_SHA256 = "b6879d50f7220b8afa1a1b6b6266e27db6c3492a8fa2bc71747656c6411af449"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Counter = Annotated[int, Field(ge=0)]


class EvidenceError(ValueError):
    """Safe failure code; never expose receipt bodies, paths or credentials."""


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


class Artifact(Record):
    component: BundleComponent
    kind: Text
    notice_hashes: dict[Text, Digest]
    native_hashes: dict[Text, Text]
    revision: Text | None


class Index(Record):
    run_url: Text
    file: Text
    sha256: Digest
    content: Text


class Receipt(Record):
    run_id: Text
    file: Text
    source_sha256: Digest
    source_bytes: Counter
    selection: list[Text | Literal[""]]
    encoding: Literal["original_utf8", "selected_json_members", "json_pointer_values"]
    payload_sha256: Digest
    payload: Text


class Recipe(Record):
    source_path: Text
    sha256: Digest
    content: Text
    first_line: Annotated[int, Field(ge=1)]
    last_line: Annotated[int, Field(ge=1)]


class Group(Record):
    source_commit: Text
    review_receipt_sha256: Digest
    group_id: Text
    name: Text
    receipt_ids: list[Text]
    limits: Text


class Export(Record):
    export_id: Text
    source_files: dict[Text, Digest]
    consumer: Text
    supported: Text
    unproved: Text
    receipt_ids: list[Text]


class Unsupported(Record):
    target: Text
    consumer: Text
    reason: Text


class LiveCase(Record):
    case_id: Text
    group_id: Text
    recipe: Text
    receipt_id: Text
    request_path: Text
    actor_role: Text
    catalog: Text
    client: Text
    notebook_path: Text
    expected_total: Counter


class Evidence(Record):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    profile: BundleProfile
    qualification_scope: Text
    source_commit: Text
    observed_at: Text
    artifacts: list[Artifact]
    sources: dict[Text, Digest]
    indexes: dict[Text, Index]
    receipts: dict[Text, Receipt]
    recipes: dict[Text, Recipe]
    groups: list[Group]
    exports: list[Export]
    unsupported: list[Unsupported]
    oracles: dict[str, Any]
    live_case: LiveCase
    evidence_policy: Text

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: str) -> str:
        """Require an actual timezone-aware receipt timestamp."""
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("timezone required")
        return value


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def strict_json(value: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite number")
        return number

    return json.loads(value, object_pairs_hook=pairs, parse_constant=finite, parse_float=finite)


def retained_evidence(raw: bytes, profile_id: str) -> Evidence:
    """Verify the reviewed immutable export before interpreting native payloads."""
    if profile_id != PROFILE_ID:
        raise EvidenceError("unsupported_profile")
    try:
        raw = bytes(raw)
        evidence = Evidence.model_validate(strict_json(raw))
        if digest(raw) != FIXTURE_SHA256:
            raise EvidenceError("changed_evidence")
        for index in evidence.indexes.values():
            if digest(index.content.encode()) != index.sha256:
                raise EvidenceError("index_mismatch")
        for receipt in evidence.receipts.values():
            entries = strict_json(evidence.indexes[receipt.run_id].content)
            entries = entries.get("files", entries)
            if isinstance(entries, list):
                entries = {entry["name"]: entry for entry in entries}
            entry = entries[receipt.file]
            expected = entry if isinstance(entry, str) else entry["sha256"]
            if receipt.source_sha256 != expected:
                raise EvidenceError("receipt_mismatch")
            payload = receipt.payload.encode()
            if digest(payload) != receipt.payload_sha256:
                raise EvidenceError("payload_mismatch")
            if receipt.encoding == "original_utf8" and (
                digest(payload) != receipt.source_sha256 or len(payload) != receipt.source_bytes
            ):
                raise EvidenceError("original_bytes_mismatch")
            strict_json(payload)
        for export in evidence.exports:
            if any(evidence.sources[path] != sha for path, sha in export.source_files.items()):
                raise EvidenceError("export_mismatch")
            if any(ref not in evidence.receipts for ref in export.receipt_ids):
                raise EvidenceError("missing_receipt")
        for group in evidence.groups:
            if not group.receipt_ids or any(
                ref not in evidence.receipts for ref in group.receipt_ids
            ):
                raise EvidenceError("missing_receipt")
        manifest = load_bundle_manifest()
        declared = next(p for p in manifest.profiles if p.profile_id == PROFILE_ID)
        components = {c.component_id: c for c in manifest.components}
        if declared != evidence.profile or declared.status != "candidate":
            raise EvidenceError("profile_mismatch")
        if [a.component.component_id for a in evidence.artifacts] != declared.component_ids:
            raise EvidenceError("artifact_mismatch")
        if any(components[a.component.component_id] != a.component for a in evidence.artifacts):
            raise EvidenceError("artifact_mismatch")
        return evidence
    except (ValueError, KeyError, TypeError, StopIteration, RecursionError):
        raise EvidenceError("invalid_retained_evidence") from None


def native(evidence: Evidence, receipt_id: str, pointer: str = "") -> Any:
    """Read only authenticated original values; selections use explicit JSON pointers."""
    receipt = evidence.receipts[receipt_id]
    value = strict_json(receipt.payload)
    if receipt.encoding == "json_pointer_values":
        prefix = max((p for p in value if pointer == p or pointer.startswith(p + "/")), key=len)
        value, pointer = value[prefix], pointer[len(prefix) :]
    for key in pointer.split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


class OwnedLiveProfile(Record):
    """Owner must separately verify installed artifacts and authorize this owned case."""

    profile_id: Text
    case_id: Text
    evidence_sha256: Digest
    base_url: Annotated[str, Field(pattern=r"^http://127\.0\.0\.1:[0-9]{4,5}$")]
    project_uuid: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    actor_uuid: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    captured_input_sha256: Digest


def assert_live_lightdash(
    *,
    authorization: str | None = None,
    owned_profile: OwnedLiveProfile | None = None,
    session_cookie: str | None = None,
) -> dict[str, str]:
    """Opt in to one real submit/poll assertion; starts/installs nothing.

    Future owned-run caller supplies explicit scoped authorization, independently
    verified exact artifacts, original captured-input binding and an ordinary actor
    session. No authorization/profile/session means UNRUN, never success or pytest skip.
    A selected live pass is not full qualification; errors omit transport details.
    """
    if (
        not authorization
        or not authorization.strip()
        or owned_profile is None
        or not session_cookie
    ):
        return {"status": "UNRUN", "code": "authorization_profile_or_session_absent"}
    evidence = retained_evidence(FIXTURE.read_bytes(), owned_profile.profile_id)
    profile = OwnedLiveProfile.model_validate(owned_profile.model_dump())
    parsed = urlparse(profile.base_url)
    if (
        profile.case_id != evidence.live_case.case_id
        or profile.evidence_sha256 != FIXTURE_SHA256
        or profile.captured_input_sha256 != evidence.receipts["producer"].source_sha256
        or parsed.port is None
        or parsed.port < 1024
    ):
        raise EvidenceError("unproved_live_profile")

    def request(method, path, body=None):
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        connection = HTTPConnection("127.0.0.1", parsed.port, timeout=15)
        try:
            connection.request(
                method,
                path,
                body=data,
                headers={
                    "Cookie": session_cookie,
                    "Content-Type": "application/json",
                    "Origin": profile.base_url,
                },
            )
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 2 * 1024 * 1024:
                raise EvidenceError("native_request_failed")
            payload = strict_json(raw)
            if payload["status"] != "ok":
                raise EvidenceError("native_request_failed")
        finally:
            connection.close()
        return payload["results"]

    try:
        identity = request("GET", "/api/v1/user")
        if identity["userUuid"] != profile.actor_uuid or identity["role"] != "member":
            raise EvidenceError("wrong_native_actor")
        query = native(evidence, "restore-api", "/restoreIdentity")["metricQuery"]
        base = "/api/v2/projects/" + profile.project_uuid + "/query/"
        job = request(
            "POST",
            base + "metric-query",
            {"query": query, "context": "exploreView", "invalidateCache": True},
        )
        query_id = job["queryUuid"]
        if not isinstance(query_id, str) or str(UUID(query_id)) != query_id:
            raise EvidenceError("invalid_native_query_id")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = request("GET", base + query_id)
            if result["status"] == "ready":
                expected = sum(row[2] for row in evidence.oracles["orders"])
                rows = result["rows"]
                if result["queryUuid"] != query_id or len(rows) != 1:
                    raise EvidenceError("native_result_mismatch")
                raw = rows[0]["orders_total_amount"]["value"]["raw"]
                if type(raw) not in (int, str) or str(raw) != str(expected):
                    raise EvidenceError("native_result_mismatch")
                return {"status": "PASS_SELECTED_LIVE_ASSERTION", "query_uuid": query_id}
            if result["status"] not in {"pending", "queued", "executing"}:
                raise EvidenceError("native_query_failed")
            time.sleep(0.5)
        raise EvidenceError("native_query_timeout")
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        raise EvidenceError("live_assertion_failed") from None


@pytest.fixture
def evidence():
    return retained_evidence(FIXTURE.read_bytes(), PROFILE_ID)


def test_profile_pins_all_admitted_artifacts(evidence):
    assert len(evidence.artifacts) == 77
    assert {g.group_id for g in evidence.groups} == {f"G{i:02}" for i in range(1, 13)}
    assert len(evidence.exports) == 5
    assert evidence.profile.status == "candidate" and evidence.profile.qualification_receipt is None
    for artifact in evidence.artifacts:
        component = artifact.component
        assert component.version and component.sha256
        assert component.artifact_ref and component.license_expression
        assert component.status == "observed_component" and "latest" not in component.artifact_ref
    assert {a.kind for a in evidence.artifacts} >= {
        "image",
        "extension",
        "python",
        "driver",
        "adapted source",
    }
    assert {u.target for u in evidence.unsupported} >= {
        "python315",
        "windows",
        "macos-arm64",
        "production-distribution",
        "released-dlt132",
        "python-models",
    }


def test_receipt_hashes_match_exact_bundle(evidence):
    assert len(evidence.receipts) == 52 and len(evidence.indexes) == 10
    for recipe in evidence.recipes.values():
        assert evidence.sources[recipe.source_path] == recipe.sha256
        assert len(recipe.content.splitlines()) == recipe.last_line - recipe.first_line + 1
    for receipt in evidence.receipts.values():
        assert receipt.selection and receipt.source_bytes > 0
        assert evidence.indexes[receipt.run_id].run_url.endswith("/" + receipt.run_id)


def test_native_changed_seams_match_independent_oracles(evidence):
    def n(rid, pointer=""):
        return native(evidence, rid, pointer)

    rows = evidence.oracles["orders"]
    assert sum(row[2] for row in rows) == 350
    assert sum(row[2] for row in rows if "2026-09-01" <= row[1] < "2026-10-01") == 300
    assets = n("dbt-assets")
    assert all(v["exit"] == (1 if k == "python" else 0) for k, v in assets["stages"].items())
    sql = " ".join(assets["incremental_native_sql"]).lower().replace('"', "")
    assert (
        "delete from lake.assets.asset_incremental" in sql
        and "insert into lake.assets.asset_incremental" in sql
    )
    for rid in ["ordinary-bi", "identity-bi"]:
        assert [
            int(q["result"]["rows"][0]["orders_total_amount"]["value"]["raw"])
            for q in n(rid)["queries"]
        ] == [350, 300]
        assert n(rid)["privateResult"]["editorStatus"] == 403
    for rid, pointer, expected in evidence.oracles["native_answers"]:
        assert n(rid, pointer) == expected
    phases = n("namespaces", "/phases")
    for phase, totals in zip(
        phases.values(), [(350, 7, 350), (350, 7, 700), (360, 8, 700), (350, 7, 700)], strict=True
    ):
        a, b = phase["alice"], phase["bob"]
        assert (*a["tuple"], b["tuple"][0]) == totals
        assert a["namespace_oid"] != b["namespace_oid"] and a["data_root"] != b["data_root"]
        assert {r[3] for r in a["table_ids"]}.isdisjoint({r[3] for r in b["table_ids"]})
    producer = n("producer")
    assert producer["raw_first"]["rows"] == rows and producer["raw_second"]["total"] == 360
    assert producer["captured_before"] == producer["captured_after"]
    held = n("held")
    assert held["held"]["sql_sha256"] == held["fresh"]["sql_sha256"]
    assert held["held"]["queryUuid"] != held["fresh"]["queryUuid"]
    assert held["executionWitness"]["blockers"] == [held["lock"]["owner_pid"]]
    assert held["fresh"]["snapshot"] == held["held"]["snapshot"] + 1
    for i, case in enumerate(evidence.oracles["semantic_cases"]):
        prefix = "/semantic/queries/" + str(i)
        assert n("semantic", prefix + "/name") == case["name"]
        assert sorted(n("semantic", prefix + "/actual"), key=str) == sorted(
            case["expected"], key=str
        )
        assert digest(n("semantic", prefix + "/compiledSQL").encode()) == n(
            "semantic", prefix + "/sql_sha256"
        )
    for client, pointer in [("engine-python", "/cases"), ("engine-node", "/core")]:
        actual = {case["id"]: case for case in n(client, pointer)}
        for case in evidence.oracles["engine_cases"]["cases"]:
            observed = actual[case["id"]]
            if "expected" in case:
                assert observed["actual"] == case["expected"]
            else:
                message = observed.get("message", observed.get("native_error", {}).get("message"))
                assert case["error"]["message_contains"] in message
    for rid in ["n3-refuse-old", "n3-catalog-down", "n3-catalog-up"]:
        assert n(rid, "/before") == n(rid, "/after")
    assert [
        int(q["result"]["rows"][0]["orders_total_amount"]["value"]["raw"])
        for q in n("restore")["restored"]["queries"]
    ] == [350, 300]


def test_unqualified_or_missing_profile_not_passed(evidence):
    manifest = load_bundle_manifest()
    for profile in manifest.profiles:
        with pytest.raises(BundleValidationError) as error:
            require_qualified_profile(manifest, profile.profile_id)
        assert error.value.code == "unqualified_profile"
    with pytest.raises(BundleValidationError) as error:
        require_qualified_profile(manifest, "missing-profile")
    assert error.value.code == "unsupported_profile"
    for target in ["", "missing", "python-3-15-duckdb-2-target", "dated-macos-arm64-candidate"]:
        with pytest.raises(EvidenceError):
            retained_evidence(FIXTURE.read_bytes(), target)
    assert (
        assert_live_lightdash(owned_profile=OwnedLiveProfile.model_construct())["status"] == "UNRUN"
    )
    assert (
        assert_live_lightdash(authorization="future owned run", session_cookie="synthetic-unrun")[
            "status"
        ]
        == "UNRUN"
    )
    data = strict_json(FIXTURE.read_bytes())
    for mutation in [
        lambda d: d["receipts"].pop("held"),
        lambda d: d["receipts"]["held"].update(payload="{}"),
        lambda d: d["artifacts"][0]["component"].update(sha256="0" * 64),
        lambda d: d["profile"].update(status="qualified", qualification_receipt="nonempty"),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(extra="synthetic"),
    ]:
        changed = strict_json(json.dumps(data))
        mutation(changed)
        with pytest.raises(EvidenceError):
            retained_evidence(json.dumps(changed).encode(), PROFILE_ID)
    with pytest.raises(ValidationError):
        Evidence.model_validate({**data, "observed_at": "2026-11-23T00:00:00"})


def test_api_fixtures_are_redacted_and_version_bound(evidence):
    text = FIXTURE.read_text()
    forbidden = ["/Users/", "release-planning/", "DANGO110-", "BEGIN PRIVATE KEY"]
    forbidden += ["ghp_", "github_pat_", "Set-Cookie", "Cookie:"]
    assert not any(marker in text for marker in forbidden)
    bi = native(evidence, "identity-bi")
    paths = {r["path"] for r in bi["requests"]}
    assert "/api/v1/invite-links" in paths and "/api/v1/login" in paths
    assert any("/api/v2/projects/" in p and p.endswith("/metric-query") for p in paths)
    assert bi["ordinaryActor"]["orgRole"] == "member"
    assert bi["reviewerPermissionProbe"]["sharedResult"] == 350
    assert bi["reviewerPermissionProbe"]["deniedAfter"]["status"] == 403
    assert bi["cli"]["genuine_dbt_version"] == "dbt-oss 2.0.8"
    assert bi["cli"]["supported_metadata_fallback"] == "v1.12"
    metadata = native(evidence, "42825753494:catalog.json")["metadata"]
    assert metadata["dbt_schema_version"] == "https://schemas.getdbt.com/dbt/catalog/v1.json"
    assert metadata["dbt_version"] == "2.0.8"
    for bad in ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}']:
        with pytest.raises(ValueError):
            strict_json(bad)
