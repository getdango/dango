"""tests/integration/test_scheduled_dbt_failure_sinks.py

A failed scheduled dbt run (real `run_scheduled_dbt`, real dbt + DuckDB, no Docker) hands every
sink (activity log, run record, execution log, WebSocket event, notifier) one short redacted
reason line instead of dbt's raw multi-line output, and never a planted secret.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from dango.cli.commands.source import sync
from dango.platform.scheduling import jobs
from tests.integration.dbt_outcome_support import make_csv_project, write_orders_csv

pytestmark = pytest.mark.integration

CLEAN_CSV = "id,name\n1,a\n2,b\n3,c\n"
DUPLICATE_CSV = "id,name\n1,a\n2,b\n3,c\n3,dup\n"
SECRET = "Zq9xKd4417livetoken"
LINE_LIMIT = 300
PREFIX = "Scheduled dbt failed: "


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DANGO_TELEMETRY", "false")
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)


class Sinks:
    """What each sink of the scheduled dbt job received."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.broadcast: list[dict[str, Any]] = []
        self.notify: list[dict[str, Any]] = []
        self.record: list[dict[str, Any]] = []
        self.execution: list[dict[str, Any]] = []

    def activity(self) -> list[str]:
        log = self.root / ".dango" / "logs" / "activity.jsonl"
        rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]
        return [r["message"] for r in rows if r["level"] == "error"]

    def error_texts(self) -> dict[str, list[str]]:
        failed = [m for m in self.broadcast if m.get("event") == "dbt_failed"]
        return {
            "activity": [m.removeprefix(PREFIX) for m in self.activity()],
            "record": [str(r["error"]) for r in self.record],
            "execution": [str(r["error"]) for r in self.execution],
            "broadcast": [str(m["error"]) for m in failed],
            "notify": [str(n["error"]) for n in self.notify],
        }


@pytest.fixture
def sinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sinks:
    out = Sinks(tmp_path / "proj")
    monkeypatch.setattr(jobs, "_broadcast", out.broadcast.append)
    monkeypatch.setattr(jobs, "_notify", lambda _sender, **kw: out.notify.append(kw))
    monkeypatch.setattr(
        jobs, "_try_finish_record", lambda _r, _s, _id, _fn, **kw: out.record.append(kw)
    )
    monkeypatch.setattr(jobs, "_log_execution_event", lambda **kw: out.execution.append(kw))
    return out


def _cli_sync(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    CliRunner().invoke(sync, ["--yes"], obj={"project_root": root})


def _project(sinks: Sinks, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose first sync built the staging layer cleanly; `.env` holds a secret."""
    root = make_csv_project(sinks.root, CLEAN_CSV)
    (root / ".env").write_text(f"SHOP_API_TOKEN={SECRET}\n", encoding="utf-8", newline="\n")
    _cli_sync(root, monkeypatch)
    return root


def _add_model(root: Path, name: str, sql: str) -> None:
    (root / "dbt" / "models" / "marts" / f"{name}.sql").write_text(
        sql + "\n", encoding="utf-8", newline="\n"
    )


def _run_scheduled(root: Path) -> None:
    jobs.run_scheduled_dbt("nightly", None, project_root=str(root))


def _assert_one_redacted_line(sinks: Sinks) -> dict[str, list[str]]:
    texts = sinks.error_texts()
    for sink, values in texts.items():
        assert not any(SECRET in v for v in values), f"secret reached the {sink} sink"
    for sink, values in texts.items():
        assert len(values) == 1, (sink, values)
        text = values[0]
        assert text and "\n" not in text, (sink, text)
        assert len(text) <= LINE_LIMIT, (sink, len(text))
        assert SECRET not in text, sink
    assert SECRET not in (sinks.root / ".dango" / "logs" / "activity.jsonl").read_text("utf-8")
    assert len({v[0] for v in texts.values()}) == 1  # every sink gets the same reason
    return texts


def test_duplicate_key_failure_reaches_every_sink_as_one_line(
    sinks: Sinks, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(sinks, monkeypatch)
    write_orders_csv(root, DUPLICATE_CSV)
    _cli_sync(root, monkeypatch)  # loads the duplicate: its own dbt run fails (not under test)
    _run_scheduled(root)
    texts = _assert_one_redacted_line(sinks)
    assert "source_unique_shop_shop_id" in texts["broadcast"][0]
    assert sinks.activity()[0].startswith(PREFIX)
    assert [m["event"] for m in sinks.broadcast][-1] == "dbt_failed"
    assert {"event", "schedule", "error", "timestamp"} <= set(sinks.broadcast[-1])


def test_model_error_with_a_planted_secret_is_redacted_in_every_sink(
    sinks: Sinks, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(sinks, monkeypatch)
    _add_model(root, "broken_secret", f"select cast('{SECRET}' as integer) as x")
    _run_scheduled(root)
    texts = _assert_one_redacted_line(sinks)
    assert "broken_secret" in texts["notify"][0]


def test_compile_error_naming_a_secret_is_redacted_in_every_sink(
    sinks: Sinks, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(sinks, monkeypatch)
    _add_model(root, "bad_ref", f"select * from {{{{ ref('{SECRET}') }}}}")
    _run_scheduled(root)
    texts = _assert_one_redacted_line(sinks)
    assert "Compilation Error" in texts["activity"][0]
