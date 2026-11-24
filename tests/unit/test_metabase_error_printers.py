"""tests/unit/test_metabase_error_printers.py

Every printer of a Metabase save, load, provision, refresh, start-import or serve-setup result must
print error text redacted, on one line per item, cut, escaped and capped, without changing exit codes.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from dango.cli.commands.dashboard import dashboard_provision
from dango.cli.commands.metabase_cmd import metabase_load, metabase_refresh, metabase_save
from dango.cli.commands.serve import serve
from tests.unit.test_start_metabase_setup_failure_message import _failed, _run_start

_URL_SECRET = "http://admin:hunter2secret@metabase.internal:3000/api"
_TOKEN = "sk_live_abcdefghijklmnop1234"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# (exit code, everything the command printed)
Run = Callable[[Path, Any, Console], tuple[int, str]]


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    """Undo what the real commands put in ``os.environ`` (``serve`` sets DANGO_PROJECT_ROOT, and
    the startup prologue loads the project's ``.env``), so later tests in the process see none of it."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _console(stream: io.TextIOBase) -> Console:
    return Console(file=stream, force_terminal=False, width=10000, legacy_windows=False)


def _text(console: Console) -> str:
    """Everything printed so far (empty when the console writes to a non-StringIO stream)."""
    return console.file.getvalue() if isinstance(console.file, io.StringIO) else ""


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _save_result(errors: Any) -> dict[str, Any]:
    return {
        "success": False,
        "errors": errors,
        "exported_dashboards": [],
        "exported_questions": [],
        "skipped_collections": [],
    }


def _load_result(errors: Any) -> dict[str, Any]:
    return {
        "success": False,
        "errors": errors,
        "imported_dashboards": [],
        "imported_questions": [],
        "skipped": [],
        "would_overwrite": [],
    }


def _run_manager_command(
    command: Any, method: str, result: dict[str, Any], root: Path, console: Console
) -> int:
    manager = MagicMock()
    getattr(manager, method).return_value = result
    with (
        patch("dango.cli.utils.require_project_context", return_value=root),
        patch("dango.visualization.dashboard_manager.DashboardManager", return_value=manager),
        patch("dango.cli.commands.metabase_cmd.console", console),
    ):
        return CliRunner().invoke(command, obj={}).exit_code


def _run_save(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    code = _run_manager_command(metabase_save, "save_to_files", _save_result(errors), root, console)
    return code, _text(console)


def _run_load(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    code = _run_manager_command(
        metabase_load, "load_from_files", _load_result(errors), root, console
    )
    return code, _text(console)


def _provision(root: Path, result: dict[str, Any], console: Console) -> tuple[int, str]:
    args = ["--url", "http://localhost:3999", "--username", "a@example.com", "--password", "pw"]
    with (
        patch("dango.visualization.provision_dashboard", return_value=result),
        patch("dango.visualization.metabase.refresh_metabase_connection"),
        patch("dango.utils.pipeline_health.materialize_pipeline_health"),
        patch("dango.cli.commands.dashboard.console", console),
    ):
        code = CliRunner().invoke(dashboard_provision, args, obj={"project_root": root}).exit_code
    return code, _text(console)


def _run_provision_failed(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    return _provision(root, {"success": False, "errors": errors}, console)


def _run_provision_warnings(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    ok = {
        "success": True,
        "errors": errors,
        "dashboard_id": 7,
        "dashboard_url": "http://localhost:3999/dashboard/7",
        "cards_created": [],
    }
    return _provision(root, ok, console)


def _run_refresh(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    metadata = {"metabase_url": "http://localhost:3999", "database": {"id": 1}}
    with (
        patch("dango.cli.utils.require_project_context", return_value=root),
        patch("dango.cli.commands.metabase_cmd.load_metabase_metadata", return_value=metadata),
        patch("requests.get", return_value=MagicMock(status_code=200)),
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(False, errors, None),
        ),
        patch("dango.cli.commands.metabase_cmd.console", console),
    ):
        code = CliRunner().invoke(metabase_refresh, obj={}).exit_code
    return code, _text(console)


def _run_start_import(root: Path, errors: Any, console: Console) -> tuple[int, str]:
    result, _ = _run_start(
        root, _failed(True, []), console, import_result={"imported": [], "errors": errors}
    )
    return result.exit_code, _text(console)


def _run_serve(
    root: Path, errors: Any, console: Console, *, raises: Exception | None = None
) -> tuple[int, str]:
    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    loader = MagicMock()
    loader.load_config.return_value = config
    startup = "dango.platform.common.startup"
    setup = {"success": False, "duckdb_connected": False, "errors": errors}
    with ExitStack() as stack:
        stack.enter_context(patch("uvicorn.run"))
        stack.enter_context(patch("dango.cli.commands.serve._check_port"))
        stack.enter_context(patch(f"{startup}.import_dashboards"))
        stack.enter_context(
            patch(f"{startup}.setup_metabase_if_needed", return_value=setup, side_effect=raises)
        )
        for name in ("start_docker_services", "ensure_duckdb_driver", "ensure_dbt_schemas"):
            stack.enter_context(patch(f"{startup}.{name}"))
        stack.enter_context(patch(f"{startup}.run_pending_migrations", return_value={}))
        stack.enter_context(patch("dango.config.ConfigLoader", return_value=loader))
        stack.enter_context(patch("dango.cli.utils.require_project_context", return_value=root))
        result = CliRunner().invoke(serve, [], obj={})
    return result.exit_code, result.output


# Sites that print every item of the result's ``errors`` value, with the exit code they must keep.
LIST_SITES: dict[str, tuple[Run, int]] = {
    "save": (_run_save, 0),
    "load": (_run_load, 0),
    "provision-failed": (_run_provision_failed, 1),
    "provision-warnings": (_run_provision_warnings, 0),
    "serve": (_run_serve, 0),
}
# Sites that print one error text.
SINGLE_SITES: dict[str, tuple[Run, int]] = {
    "refresh": (_run_refresh, 1),
    "start-import": (_run_start_import, 0),
}
ALL_SITES = {**LIST_SITES, **SINGLE_SITES}


def _run(site: str, root: Path, errors: Any, console: Console | None = None) -> tuple[int, str]:
    run, expected = ALL_SITES[site]
    code, text = run(root, errors, console or _console(io.StringIO()))
    return code, _ANSI_RE.sub("", text)


def _errors_for(site: str, items: list[Any]) -> Any:
    """What the site is fed: the list itself, or its first item for the one-text sites."""
    return items if site in LIST_SITES else items[0]


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
@pytest.mark.parametrize(
    "text",
    [
        "bad [red]thing[/red] here",
        "unbalanced [ bracket [/oops]",
        "[bold]never closed",
        "see [link=http://example.test]here[/link]",
    ],
)
def test_markup_is_printed_literally_and_keeps_the_exit_code(
    tmp_path: Path, site: str, text: str
) -> None:
    code, out = _run(site, tmp_path, _errors_for(site, [f"Failed: {text}"]))

    assert code == ALL_SITES[site][1], out
    assert f"Failed: {text}" in _flat(out)


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
def test_credentials_and_tokens_never_reach_the_output(tmp_path: Path, site: str) -> None:
    (tmp_path / ".env").write_text("MY_SECRET_VALUE=zebra-quartz-93417\n", encoding="utf-8")
    items = [
        f"could not reach {_URL_SECRET}",
        f"failed with api_key={_TOKEN} and zebra-quartz-93417 rejected",
    ]
    code, out = _run(site, tmp_path, _errors_for(site, items))

    assert code == ALL_SITES[site][1], out
    assert "hunter2secret" not in out
    assert _TOKEN not in out
    assert "zebra-quartz-93417" not in out
    assert "metabase.internal" in out


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
def test_terminal_control_characters_are_not_printed(tmp_path: Path, site: str) -> None:
    errors = _errors_for(site, ["ok \x1b[31mred\x1b[0m \x1b]0;title\x07 end"])
    code, out = ALL_SITES[site][0](tmp_path, errors, _console(io.StringIO()))

    assert code == ALL_SITES[site][1]
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "red" in out


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
def test_newlines_and_tabs_keep_one_item_on_one_line(tmp_path: Path, site: str) -> None:
    code, out = _run(site, tmp_path, _errors_for(site, ["first-part\nsecond\tpart\r\nthird"]))

    assert code == ALL_SITES[site][1], out
    lines = [ln for ln in out.splitlines() if "first-part" in ln]
    assert len(lines) == 1
    assert "second" in lines[0]
    assert "third" in lines[0]


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
def test_very_long_error_is_cut(tmp_path: Path, site: str) -> None:
    code, out = _run(site, tmp_path, _errors_for(site, ["Failed: " + "x" * 20000]))

    assert code == ALL_SITES[site][1], out
    assert "x" * 100 in out
    assert "x" * 500 not in out
    assert "truncated" in out
    assert all(len(line) < 700 for line in out.splitlines() if "xxxx" in line)


@pytest.mark.unit
@pytest.mark.parametrize("site", LIST_SITES)
def test_items_beyond_the_cap_are_summarised(tmp_path: Path, site: str) -> None:
    items = [f"step-{i}-failed" for i in range(20)]
    code, out = _run(site, tmp_path, items)

    assert code == ALL_SITES[site][1], out
    assert "step-7-failed" in out
    assert "step-8-failed" not in out
    assert "... and 12 more" in out


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
def test_non_ascii_text_survives_a_cp1252_stdout_with_replace(tmp_path: Path, site: str) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="replace", write_through=True)
    run, expected = ALL_SITES[site]
    code, _ = run(tmp_path, _errors_for(site, ["café ✓ 日本 failed"]), _console(stream))

    stream.flush()
    assert code == expected


@pytest.mark.unit
@pytest.mark.parametrize("site", LIST_SITES)
@pytest.mark.parametrize("errors", [None, [], "", [None, ""], 0])
def test_empty_errors_print_without_crashing(tmp_path: Path, site: str, errors: Any) -> None:
    code, out = _run(site, tmp_path, errors)

    assert code == ALL_SITES[site][1], out


@pytest.mark.unit
@pytest.mark.parametrize("site", LIST_SITES)
def test_a_string_instead_of_a_list_is_one_item(tmp_path: Path, site: str) -> None:
    code, out = _run(site, tmp_path, "whole failure text")

    assert code == ALL_SITES[site][1], out
    assert "whole failure text" in _flat(out)


@pytest.mark.unit
@pytest.mark.parametrize("site", LIST_SITES)
def test_non_string_items_are_printed_as_text(tmp_path: Path, site: str) -> None:
    items = [{"detail": "bad [/x]"}, 42, ValueError("[/boom] http://u:pw1234secret@h/x")]
    code, out = _run(site, tmp_path, items)

    assert code == ALL_SITES[site][1], out
    assert "bad [/x]" in out
    assert "42" in out
    assert "[/boom]" in out
    assert "pw1234secret" not in out


@pytest.mark.unit
def test_unbalanced_markup_no_longer_hides_the_real_error_in_save(tmp_path: Path) -> None:
    code, out = _run("save", tmp_path, ["Failed to write board [/oops]"])

    assert code == 0
    assert "No assets exported" in out
    assert "Failed to write board [/oops]" in out
    assert "closing tag" not in out


@pytest.mark.unit
def test_provision_failure_still_aborts_with_troubleshooting(tmp_path: Path) -> None:
    code, out = _run("provision-failed", tmp_path, ["[/oops]"])

    assert code == 1
    assert "[/oops]" in out
    assert "Troubleshooting" in out


@pytest.mark.unit
def test_serve_warning_keeps_its_wording(tmp_path: Path) -> None:
    code, out = _run("serve", tmp_path, ["connection refused", "second"])

    assert code == 0
    assert "WARNING: Metabase setup incomplete: connection refused; second. " in out
    assert "Continuing without Metabase" in out
    assert "retry on next restart." in out


@pytest.mark.unit
def test_serve_warning_without_errors_says_unknown(tmp_path: Path) -> None:
    _, out = _run("serve", tmp_path, [])

    assert "WARNING: Metabase setup incomplete: unknown. " in out


@pytest.mark.unit
def test_serve_warning_for_a_raised_setup_error_is_redacted_and_bounded(tmp_path: Path) -> None:
    boom = RuntimeError(f"crash at {_URL_SECRET} api_key={_TOKEN} " + "y" * 20000)
    code, out = _run_serve(tmp_path, None, _console(io.StringIO()), raises=boom)

    assert code == 0
    assert "WARNING: Metabase setup failed: " in out
    assert "hunter2secret" not in out
    assert _TOKEN not in out
    assert "y" * 500 not in out
    assert "Continuing without Metabase" in out


@pytest.mark.unit
def test_ordinary_error_output_is_unchanged(tmp_path: Path) -> None:
    assert (
        "No assets exported\n\n  • Failed to write board: boom\n"
        in _run("save", tmp_path, ["Failed to write board: boom"])[1]
    )
    assert "Nothing to import\n\n  • one\n  • two\n" in _run("load", tmp_path, ["one", "two"])[1]
    assert "Errors:\n  • one\n  • two\n" in _run("provision-failed", tmp_path, ["one", "two"])[1]
    assert "Warnings:\n  • one\n" in _run("provision-warnings", tmp_path, ["one"])[1]
    assert "Could not refresh Metabase connection: boom\n" in _run("refresh", tmp_path, "boom")[1]


@pytest.mark.unit
def test_success_output_of_save_and_provision_is_unchanged(tmp_path: Path) -> None:
    saved = {
        "success": True,
        "errors": [],
        "exported_dashboards": [{"name": "Board", "cards": 2, "collection": "Shared"}],
        "exported_questions": [{"name": "Q", "type": "question", "collection": "Shared"}],
        "skipped_collections": [],
    }
    console = _console(io.StringIO())
    assert _run_manager_command(metabase_save, "save_to_files", saved, tmp_path, console) == 0
    out = _text(console)
    assert "✅ Exported 2 item(s) to metabase/\n" in out
    assert "Dashboards:\n  ✓ Board (2 cards) - Shared\n" in out
    assert "Questions:\n  ✓ Q (question) - Shared\n" in out

    ok = {
        "success": True,
        "errors": [],
        "dashboard_id": 7,
        "dashboard_url": "http://x/7",
        "cards_created": [],
    }
    code, text = _provision(tmp_path, ok, _console(io.StringIO()))
    assert code == 0
    assert "Dashboard provisioned successfully!" in text
    assert "Warnings" not in text
