"""dango/transformation/build_outcome.py

Decides from dbt's run_results.json whether a non-zero `dbt build` still transformed the data,
and words what was skipped, errored or failed. Never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dango.transformation.build_finalize import summarize_run_results

NOTICE_PREFIX = "dango: "
# The note of a failed build whose run results explain nothing (also tells callers there is no cause).
UNEXPLAINED_NOTE = (
    f"{NOTICE_PREFIX}the dbt build exited with an error that its run results do not explain (they "
    "are missing, stale, unreadable or show no failed node), so the build is treated as failed."
)
ERRORED_NOTE_START = f"{NOTICE_PREFIX}failed models, seeds, snapshots or hooks"
_NAME_CAP = 5
_TEST_TYPES = frozenset({"test", "unit_test"})
_ERROR_STATUSES = frozenset({"error", "runtime error"})
_FAILED_TEST_STATUSES = frozenset({"fail", "error", "runtime error"})


@dataclass(frozen=True)
class BuildVerdict:
    """What a fresh run_results.json says about a build that exited non-zero.

    ``usable`` is False when there is no fresh, parseable result with any node in it.
    """

    usable: bool
    errored: tuple[str, ...] = ()  # models/seeds/snapshots that errored
    skipped: tuple[str, ...] = ()  # nodes dbt skipped (models first, tests only if no model)
    skipped_models: bool = False
    tests_failed: tuple[str, ...] = ()

    @property
    def transformed(self) -> bool:
        """True only for failed data tests with nothing errored or skipped: the data is built."""
        return self.usable and not self.errored and not self.skipped and bool(self.tests_failed)


def assess_failed_build(project_root: Path, *, since: float) -> BuildVerdict:
    """Classify the run_results.json written since ``since`` (epoch seconds)."""
    summary = summarize_run_results(project_root, since=since)
    if not summary or not summary["nodes"]:
        return BuildVerdict(usable=False)
    errored: list[str] = []
    skipped_models: list[str] = []
    skipped_tests: list[str] = []
    tests_failed: list[str] = []
    for node in summary["nodes"]:
        status = str(node["status"]).lower()
        is_test = node["resource_type"] in _TEST_TYPES
        if status == "skipped":
            (skipped_tests if is_test else skipped_models).append(node["name"])
        elif is_test and status in _FAILED_TEST_STATUSES:
            tests_failed.append(node["name"])
        elif not is_test and status in _ERROR_STATUSES:
            errored.append(node["name"])
    return BuildVerdict(
        usable=True,
        errored=tuple(errored),
        skipped=tuple(skipped_models or skipped_tests),
        skipped_models=bool(skipped_models),
        tests_failed=tuple(tests_failed),
    )


def _names(names: tuple[str, ...]) -> str:
    shown = ", ".join(names[:_NAME_CAP])
    return shown if len(names) <= _NAME_CAP else f"{shown} and {len(names) - _NAME_CAP} more"


def failure_notes(verdict: BuildVerdict) -> list[str]:
    """Lines naming the failed/skipped nodes and failed tests of a failed build."""
    notes: list[str] = []
    if verdict.errored:
        notes.append(f"{ERRORED_NOTE_START} ({len(verdict.errored)}): {_names(verdict.errored)}")
    if verdict.skipped:
        what = "models" if verdict.skipped_models else "tests"
        notes.append(
            f"{NOTICE_PREFIX}{what} skipped because something they depend on failed "
            f"({len(verdict.skipped)}): {_names(verdict.skipped)}"
        )
    if verdict.tests_failed:
        notes.append(
            f"{NOTICE_PREFIX}tests failed ({len(verdict.tests_failed)}): "
            f"{_names(verdict.tests_failed)}"
        )
    return notes


def tests_only_note(verdict: BuildVerdict) -> str:
    """The line appended to the output of a build whose only problem was failed data tests."""
    return (
        f"{NOTICE_PREFIX}models were built, but tests failed "
        f"({len(verdict.tests_failed)}): {_names(verdict.tests_failed)}"
    )


def notes_in(output: str) -> list[str]:
    """The notes of this module in a ``run_dbt_models`` output, as plain sentences for a terminal."""
    found = []
    for line in output.splitlines():
        if line.startswith(NOTICE_PREFIX):
            text = line[len(NOTICE_PREFIX) :].strip()
            found.append(text[:1].upper() + text[1:])
    return found


def without_notes(output: str) -> str:
    """``output`` minus the lines of this module (to print them once, separately)."""
    return "\n".join(ln for ln in output.splitlines() if not ln.startswith(NOTICE_PREFIX))
