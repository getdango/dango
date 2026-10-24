"""dango/platform/cloud/runner_isolation.py

Plan and policy for turning on restricted runner users on a cloud server.

Holds the ordered step list as pure data (what to run, as whom, how a failure is treated),
the id rules for the runner users, the marker record and how it is read back, and
:func:`run_steps`, which applies the failure policy to results supplied by an executor.
Importing this module has no side effects and nothing calls it yet: the setup and upgrade
wiring and the command line come later.

Failure policy: the marker is the last step and is never reached after a fatal failure.
A failed ``nftables`` or ``acl`` package install, or failed firewall rules, only means
``loopback: unrestricted`` plus a warning; everything about files (users, sudoers, deny
ACLs, notebook ACLs, verification) is fatal.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.run_as import MARKER, RESERVED_UIDS

__all__ = [
    "MARKER",
    "Outcome",
    "RunnerRecord",
    "Step",
    "StepResult",
    "assert_runner_ids",
    "build_steps",
    "parse_record",
    "read_record",
    "run_steps",
]

RunAs = Literal["root", "service"]
OnFailure = Literal["fatal", "warn"]

#: ``loopback`` values in the record.
RESTRICTED = scripts.LOOPBACK_RESTRICTED
UNRESTRICTED = scripts.LOOPBACK_UNRESTRICTED
_TRUSTED_UID = 0


@dataclass(frozen=True)
class Step:
    """One named, idempotent script of the enabling sequence."""

    name: str
    script: str
    run_as: RunAs = "root"
    on_failure: OnFailure = "fatal"
    #: Steps that must have succeeded; otherwise this one is not attempted.
    requires: tuple[str, ...] = ()
    #: Text added to the outcome's warnings when a ``warn`` step fails.
    warning: str = ""

    @property
    def command(self) -> str:
        """What to give a root shell: the script, or the script wrapped to run as the service user."""
        if self.run_as == "service":
            return scripts.as_service_user(self.script)
        return self.script


@dataclass(frozen=True)
class StepResult:
    """What an executor reports for one step."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Outcome:
    """Result of applying the plan."""

    enabled: bool
    loopback: str = UNRESTRICTED
    warnings: tuple[str, ...] = ()
    failed: str | None = None
    detail: str = ""
    attempted: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RunnerRecord:
    """Content of the marker file."""

    schema: int
    loopback: str


NFT_WARNING = (
    "Loopback rules for the runner users were not installed (loopback: unrestricted): "
    "scripts and notebooks can still reach Dango's local web services."
)
ACL_WARNING = "The acl package could not be installed."
NFT_PACKAGE_WARNING = "The nftables package could not be installed."


def assert_runner_ids(users: dict[str, tuple[int, int]]) -> None:
    """Check ``{name: (uid, gid)}``: ids in 1000-59999, none reserved, uids distinct.

    Raises:
        ValueError: On the first violation.
    """
    uids = []
    for name, (uid, gid) in users.items():
        for kind, value in (("uid", uid), ("gid", gid)):
            if value in RESERVED_UIDS or not scripts.UID_MIN <= value < scripts.UID_MAX:
                raise ValueError(
                    f"runner {name}: {kind} {value} is not allowed (must be "
                    f"{scripts.UID_MIN}-{scripts.UID_MAX - 1} and not in {sorted(RESERVED_UIDS)})"
                )
        uids.append(uid)
    if len(set(uids)) != len(uids):
        raise ValueError(f"runner users must have distinct uids, got {uids}")


def build_steps(srv_dir: str = scripts.SRV_DIR) -> list[Step]:
    """The enabling sequence, in order; the marker is last."""
    return [
        Step(
            "acl_package",
            scripts.package_script("acl", "setfacl"),
            on_failure="warn",
            warning=ACL_WARNING,
        ),
        Step(
            "nftables_package",
            scripts.package_script("nftables", "nft"),
            on_failure="warn",
            warning=NFT_PACKAGE_WARNING,
        ),
        Step("runner_base_dir", scripts.runner_base_dir_script(srv_dir)),
        Step("users", scripts.users_script(srv_dir)),
        Step("runner_dirs", scripts.runner_dirs_script(srv_dir)),
        Step("sudoers", scripts.sudoers_install_script()),
        Step(
            "project_permissions",
            access.project_permission_script(srv_dir=srv_dir),
            run_as="service",
        ),
        Step(
            "notebooks_acl",
            access.notebooks_acl_script(srv_dir=srv_dir),
            run_as="service",
        ),
        Step(
            "nft_rules",
            scripts.nft_install_script(),
            on_failure="warn",
            requires=("nftables_package",),
            warning=NFT_WARNING,
        ),
        Step("verify", access.verify_script(srv_dir=srv_dir)),
        Step("marker", scripts.marker_script(srv_dir), requires=("verify",)),
    ]


def run_steps(steps: Sequence[Step], execute: Callable[[Step], StepResult]) -> Outcome:
    """Apply *steps* in order through *execute* and the failure policy.

    A fatal failure ends the run at once (so the marker is never written); a ``warn``
    failure adds a warning, and steps that require it are not attempted.
    """
    warnings: list[str] = []
    done: set[str] = set()
    tried: list[str] = []
    loopback = UNRESTRICTED
    for step in steps:
        if not set(step.requires) <= done:
            if step.on_failure == "fatal":
                return Outcome(
                    False,
                    loopback,
                    tuple(warnings),
                    step.name,
                    "a required step failed",
                    tuple(tried),
                )
            if step.warning and step.warning not in warnings:
                warnings.append(step.warning)
            continue
        tried.append(step.name)
        result = execute(step)
        if result.ok:
            done.add(step.name)
            if step.name == "nft_rules":
                loopback = RESTRICTED
            continue
        if step.on_failure == "fatal":
            return Outcome(False, loopback, tuple(warnings), step.name, result.detail, tuple(tried))
        warnings.append(f"{step.warning} ({result.detail})" if result.detail else step.warning)
    return Outcome(True, loopback, tuple(warnings), None, "", tuple(tried))


def parse_record(text: str) -> RunnerRecord:
    """Parse the marker content.

    Raises:
        ValueError: If it is not the expected JSON object.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"marker is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("marker is not a JSON object")
    schema, loopback = data.get("schema"), data.get("loopback")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError("marker schema is not a positive integer")
    if loopback not in (RESTRICTED, UNRESTRICTED):
        raise ValueError(f"marker loopback is not recognised: {loopback!r}")
    return RunnerRecord(schema, loopback)


def read_record(path: str = MARKER, *, trusted_uid: int = _TRUSTED_UID) -> RunnerRecord | None:
    """Read the marker as :func:`dango.platform.run_as.uses_isolation` trusts it.

    Returns ``None`` unless the path is a regular file (not a link) owned by *trusted_uid*
    that no group or other user can write, and its content parses.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        trusted = (
            stat.S_ISREG(st.st_mode)
            and st.st_uid == trusted_uid
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
        if not trusted:
            return None
        with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
            fd = -1
            text = handle.read(4096)
    finally:
        if fd != -1:
            os.close(fd)
    try:
        return parse_record(text)
    except ValueError:
        return None
