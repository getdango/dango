"""tests/unit/test_p4c_lows.py

The four Low findings from the P4c round-2 review: the failure wording follows the real isolation
state (L-a), operator-controlled text never reaches Rich markup (L-b), every scheduled-script exit
path is labelled unisolated (L-c), and the isolation commands' upgrade-guard check names the owner,
cleans a leftover guard of the same command, runs right before arming and again right after (L-d).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.text import Text

from dango.exceptions import JobCancelledError, JobTimeoutError
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud.isolation_status import (
    IsolationStatus,
    format_isolation_line,
    parse_isolation_probe,
)
from tests.factories.remote_isolation_fakes import IsoSSH, invoke, probe
from tests.factories.runner_isolation_fakes import RunnerServer
from tests.unit.test_isolation_state import _JOBS, _fake_popen, _project, cloud, markers

pytestmark = pytest.mark.unit

__all__ = ["cloud", "markers"]

PREFIX = "dango-upgrade-guard"
OWN_DISABLE = f"{PREFIX}-isolation-disable-20261025T000000Z.timer"
OWN_ENABLE = f"{PREFIX}-isolation-enable-20261025T000000Z.timer"
UPGRADE = f"{PREFIX}-20261025T000000Z.timer"


def _plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


# --- L-a: failure wording follows the real state ------------------------------------------------


def _failed_enable(*, marker: bool, disabled: bool) -> str:
    srv = RunnerServer(in_group=False)
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"}) if marker else None
    srv.rfail = {"users": 5}
    srv.disabled_marker = disabled
    warnings: list[str] = []
    ri.enable(srv, warnings)
    return next(w for w in warnings if w.startswith(ri.ISOLATION_MARK))


def test_failure_on_a_disabled_server_does_not_claim_isolation_is_in_force() -> None:
    text = _failed_enable(marker=True, disabled=True)
    assert "still in force" not in text and "stay isolated" not in text
    assert "disabled" in text and "service account" in text


def test_failure_on_a_disabled_server_without_active_marker_says_disabled() -> None:
    text = _failed_enable(marker=False, disabled=True)
    assert "disabled" in text and "in force" not in text


def test_failure_wording_for_on_and_missing_is_unchanged() -> None:
    assert "still in force" in _failed_enable(marker=True, disabled=False)
    missing = _failed_enable(marker=False, disabled=False)
    assert "keep running as the service account" in missing and "disabled" not in missing


# --- L-b: markup in operator-controlled text -----------------------------------------------------

HOSTILE = "ali[/x]ce [bold red]boom[/]"


def _disabled_status() -> IsolationStatus:
    info = json.dumps({"schema": 1, "by": HOSTILE, "at": "2026-10-25T10:00:00+00:00 [/y]"})
    return parse_isolation_probe(probe(active="1", disabled="1", disabled_info=info))


def test_status_line_renders_the_marker_fields_as_plain_text() -> None:
    line = format_isolation_line(_disabled_status())
    assert Text.from_markup(line).plain == (
        f"disabled (admin opt-out by {HOSTILE} at 2026-10-25T10:00:00+00:00 [/y])"
    )


def test_isolation_status_command_survives_brackets_in_the_login_name() -> None:
    info = json.dumps({"schema": 1, "by": HOSTILE, "at": "2026-10-25T10:00:00+00:00 [/y]"})
    result = invoke(IsoSSH(disabled=True, disabled_info=info), ["status"])
    assert result.exit_code == 0, result.output
    assert HOSTILE in _plain(result.output) and "[/y]" in _plain(result.output)


def test_remote_status_panel_survives_brackets() -> None:
    from rich.console import Console
    from rich.panel import Panel

    line = format_isolation_line(_disabled_status())
    console = Console(record=True, width=200)
    console.print(Panel(f"  Isolation: {line}", title="Versions"))
    assert HOSTILE in console.export_text()


def test_failure_detail_from_the_server_is_printed_as_plain_text() -> None:
    ssh = IsoSSH(fail={"quiesce": 1}, fail_detail="oops [/x] [bold]")
    result = invoke(ssh, ["disable", "--yes"])
    assert result.exit_code == 1
    assert "MarkupError" not in repr(result.exception)
    assert "oops [/x] [bold]" in _plain(result.output)


def test_enable_failure_detail_is_printed_as_plain_text() -> None:
    ssh = IsoSSH(active=False, disabled=True, fail={"users": 5}, fail_detail="bad [/x]")
    result = invoke(ssh, ["enable"])
    assert result.exit_code == 1
    assert "MarkupError" not in repr(result.exception)
    assert "bad [/x]" in _plain(result.output)


def test_connect_error_with_brackets_is_printed_as_plain_text() -> None:
    result = invoke(IsoSSH(connect_error=OSError("refused [/x]")), ["status"])
    assert result.exit_code == 1 and "MarkupError" not in repr(result.exception)
    assert "refused [/x]" in _plain(result.output)


# --- L-c: every scheduled-script path is labelled ------------------------------------------------


def _run_scheduled(project: Path, *, side_effect: BaseException | None = None) -> dict[str, Any]:
    from dango.platform.scheduling.jobs import run_scheduled_script

    kwargs: dict[str, Any] = (
        {"side_effect": side_effect} if side_effect else {"return_value": ("out", "err", False)}
    )
    with (
        patch("subprocess.Popen", return_value=_fake_popen()),
        patch("dango.utils.activity_log.log_activity"),
        patch(f"{_JOBS}._broadcast"),
        patch(f"{_JOBS}._notify"),
        patch(f"{_JOBS}._try_record_start", return_value=1),
        patch(f"{_JOBS}._try_finish_record"),
        patch(f"{_JOBS}._log_execution_event"),
        patch("dango.platform.notifications.webhook.WebhookSender"),
        patch("dango.platform.notifications.webhook.load_notification_config"),
        patch(f"{_JOBS}._scheduler_service", None),
        patch("dango.platform.script_launch.communicate_with_timeout", **kwargs),
    ):
        run_scheduled_script("nightly", script_path="hello.py", project_root=str(project))
    runs = project / ".dango" / "logs" / "scripts" / "runs"
    meta: dict[str, Any] = json.loads(next(runs.glob("*/meta.json")).read_text())
    return meta


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (None, "success"),
        (JobCancelledError("cancelled mid-run"), "cancelled"),
        (JobTimeoutError("limit"), "timeout"),
        (RuntimeError("failed after start"), "failed"),
    ],
)
def test_every_path_after_start_is_labelled_unisolated(
    markers: Any, cloud: None, tmp_path: Path, error: BaseException | None, status: str
) -> None:
    markers.write(markers.disabled)
    meta = _run_scheduled(_project(tmp_path), side_effect=error)
    assert meta["status"] == status
    assert meta["unisolated"] is True


def test_a_failure_before_start_is_not_labelled(markers: Any, cloud: None, tmp_path: Path) -> None:
    markers.write(markers.disabled)
    with patch("dango.platform.script_launch.start_script", side_effect=RuntimeError("no start")):
        from dango.platform.scheduling.jobs import run_scheduled_script

        project = _project(tmp_path)
        with (
            patch(f"{_JOBS}._broadcast"),
            patch(f"{_JOBS}._notify"),
            patch(f"{_JOBS}._try_record_start", return_value=1),
            patch(f"{_JOBS}._try_finish_record"),
            patch(f"{_JOBS}._log_execution_event"),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.platform.notifications.webhook.WebhookSender"),
            patch("dango.platform.notifications.webhook.load_notification_config"),
            patch(f"{_JOBS}._scheduler_service", None),
        ):
            run_scheduled_script("nightly", script_path="hello.py", project_root=str(project))
    meta = json.loads(next((project / ".dango/logs/scripts/runs").glob("*/meta.json")).read_text())
    assert meta["status"] == "failed" and "unisolated" not in meta


# --- L-d: the upgrade-guard check -------------------------------------------------------------------


def test_own_leftover_disable_guard_does_not_block_a_rerun() -> None:
    ssh = IsoSSH(guard_units=[OWN_DISABLE])
    result = invoke(ssh, ["disable", "--yes"])
    assert result.exit_code == 0, result.output
    assert ssh.disabled and "leftover" in _plain(result.output).lower()


def test_own_leftover_enable_guard_does_not_block_a_rerun() -> None:
    ssh = IsoSSH(active=False, disabled=True, guard_units=[OWN_ENABLE])
    result = invoke(ssh, ["enable"])
    assert result.exit_code == 0, result.output
    assert not ssh.disabled


def test_a_leftover_guard_of_the_other_command_is_named_and_blocks() -> None:
    ssh = IsoSSH(guard_units=[OWN_ENABLE])
    result = invoke(ssh, ["disable", "--yes"])
    out = _plain(result.output)
    assert result.exit_code == 1 and "isolation enable" in out and "20 minutes" in out
    assert "disable_marker" not in ssh.events


def test_an_upgrade_guard_is_named_as_an_upgrade() -> None:
    ssh = IsoSSH(guard_units=[UPGRADE])
    result = invoke(ssh, ["disable", "--yes"])
    out = _plain(result.output)
    assert result.exit_code == 1 and "dango remote upgrade" in out and UPGRADE.split(".")[0] in out
    assert ssh.events == []


def test_an_upgrade_that_starts_while_the_prompt_waits_is_detected() -> None:
    ssh = IsoSSH()

    def typed(*_a: Any, **_k: Any) -> str:
        ssh.units.append(UPGRADE)
        return "disable"

    with patch("click.prompt", side_effect=typed):
        result = invoke(ssh, ["disable"])
    assert result.exit_code == 1
    assert "disable_marker" not in ssh.events and "guard:arm" not in ssh.events


def test_an_operation_that_starts_right_after_ours_stops_before_any_step() -> None:
    ssh = IsoSSH(after_arm=[UPGRADE])
    result = invoke(ssh, ["disable", "--yes"])
    assert result.exit_code == 1
    assert "dango remote upgrade" in _plain(result.output)
    assert not {"ownership_check", "stop_web", "disable_marker", "quiesce"} & set(ssh.events)
    # the other operation's guard is not ours to cancel
    assert ssh.events.count("guard:disarm") == 1 and UPGRADE in ssh.units


def test_enable_rechecks_right_after_arming() -> None:
    ssh = IsoSSH(active=False, disabled=True, after_arm=[UPGRADE])
    result = invoke(ssh, ["enable"])
    assert result.exit_code == 1
    assert "remove_disabled" not in ssh.events and ssh.disabled
    assert ssh.events.count("guard:disarm") == 1


def test_our_own_units_never_count_as_a_conflict() -> None:
    ssh = IsoSSH()
    assert invoke(ssh, ["disable", "--yes"]).exit_code == 0
    ssh = IsoSSH(active=False, disabled=True)
    assert invoke(ssh, ["enable"]).exit_code == 0


def test_the_guard_unit_name_carries_its_owner() -> None:
    ssh = IsoSSH()
    invoke(ssh, ["disable", "--yes"])
    armed = [c for c in ssh.commands if c.startswith("systemd-run")]
    assert armed and all(f"--unit={PREFIX}-isolation-disable-" in c for c in armed)
