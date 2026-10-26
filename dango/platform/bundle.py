"""dango/platform/bundle.py

Validate dated bundle candidates and refuse incomplete profile qualification.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)
from typing_extensions import Self

from dango.exceptions import DangoError


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Expected a nonempty string")
    return value


NonemptyString = Annotated[str, AfterValidator(_nonblank)]


class BundleValidationError(DangoError):
    """Safe admission failure with a stable code and an actionable next step."""

    def __init__(self, code: str, action: str) -> None:
        self.code = code
        self.action = action
        super().__init__(f"Bundle validation failed ({code}). {action}", error_code=code)


class _BundleRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


class BundleComponent(_BundleRecord):
    """An exact component observation, candidate, or bundle-qualified artifact."""

    component_id: NonemptyString
    version: NonemptyString | None
    artifact_ref: NonemptyString | None
    sha256: str | None
    edition: NonemptyString
    license_expression: NonemptyString | None
    evidence_refs: list[NonemptyString]
    status: Literal["candidate", "observed_component", "qualified_bundle"]

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        """Accept only a complete lowercase SHA-256 digest when supplied."""
        if value is not None and (
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("Expected a lowercase SHA-256 digest")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def unique_evidence(cls, values: list[str]) -> list[str]:
        """Reject repeated evidence references within a component."""
        if len(set(values)) != len(values):
            raise ValueError("Duplicate evidence reference")
        return values


class BundleProfile(_BundleRecord):
    """A platform profile whose receipt reference does not authenticate its contents.

    A qualified profile declares an immutable receipt reference. Verification of
    receipt authenticity, hashes, and actual native compatibility belongs to the
    installer/admission gate; a nonempty reference here establishes none of these.
    """

    profile_id: NonemptyString
    os: NonemptyString
    architecture: NonemptyString
    component_ids: list[NonemptyString]
    status: Literal["candidate", "qualified"]
    qualification_receipt: NonemptyString | None

    @field_validator("component_ids")
    @classmethod
    def unique_components(cls, values: list[str]) -> list[str]:
        """Reject duplicate component references within a profile."""
        if len(set(values)) != len(values):
            raise ValueError("Duplicate component reference")
        return values


class BundleManifest(_BundleRecord):
    """Schema-valid data is distinct from an authenticated, supported runtime."""

    schema_version: Literal[1]
    bundle_id: NonemptyString
    evidence_date: NonemptyString
    components: list[BundleComponent]
    profiles: list[BundleProfile]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: Any) -> Any:
        """Reject booleans and coercion before Literal equality is evaluated."""
        if type(value) is not int or value != 1:
            raise ValueError("Expected integer schema version 1")
        return value

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        """Validate identities, references, and every declared qualified profile."""
        components = {component.component_id: component for component in self.components}
        if len(components) != len(self.components):
            raise ValueError("Duplicate component ID")
        if len({profile.profile_id for profile in self.profiles}) != len(self.profiles):
            raise ValueError("Duplicate profile ID")
        for profile in self.profiles:
            if any(component_id not in components for component_id in profile.component_ids):
                raise ValueError("Unknown component reference")
            if profile.status == "qualified":
                if not profile.qualification_receipt or not profile.component_ids:
                    raise ValueError(
                        "Qualified profile requires components and a receipt reference"
                    )
                for component_id in profile.component_ids:
                    component = components[component_id]
                    if component.status != "qualified_bundle" or not all(
                        (
                            component.version,
                            component.artifact_ref,
                            component.sha256,
                            component.license_expression,
                            component.evidence_refs,
                        )
                    ):
                        raise ValueError("Qualified profile requires complete qualified artifacts")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def load_bundle_manifest(path: Path | None = None) -> BundleManifest:
    """Load a file or packaged Traversable without resolving or starting services.

    Errors deliberately omit paths, file contents, and underlying exceptions.
    Evidence and receipt references are data, never opened or authenticated here.
    """
    try:
        resource = (
            path
            if path is not None
            else files("dango").joinpath("templates").joinpath("bundle-candidates.json")
        )
        text = resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BundleValidationError(
            "missing_manifest", "Provide an existing manifest or restore the packaged resource."
        ) from None
    except (OSError, UnicodeError):
        raise BundleValidationError(
            "unreadable_manifest", "Provide a readable UTF-8 manifest or restore the package."
        ) from None
    if not text.strip():
        raise BundleValidationError("empty_manifest", "Provide a nonempty schema-1 manifest.")
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        return BundleManifest.model_validate(data)
    except (ValueError, RecursionError):
        raise BundleValidationError(
            "invalid_manifest",
            "Provide valid schema-1 JSON with unique IDs and complete qualification.",
        ) from None


def require_qualified_profile(manifest: BundleManifest, profile_id: str) -> BundleProfile:
    """Return only a declared complete qualified profile, without authenticating it.

    Revalidate at admission because frozen Pydantic records still contain mutable
    lists, and callers may have used model_copy/model_construct to bypass checks.
    Receipt authenticity and native compatibility remain separate admission gates.
    """
    try:
        checked = BundleManifest.model_validate(manifest.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise BundleValidationError(
            "invalid_manifest", "Reload a valid manifest before requesting qualification."
        ) from None
    for profile in checked.profiles:
        if profile.profile_id == profile_id:
            if profile.status != "qualified":
                raise BundleValidationError(
                    "unqualified_profile",
                    "Qualify an exact complete profile before runtime admission.",
                )
            return profile
    raise BundleValidationError(
        "unsupported_profile",
        "Choose a profile declared in the manifest and verify its qualification.",
    )
