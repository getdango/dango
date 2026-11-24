"""tests/unit/test_scheduled_dbt_failure_text.py

The scheduled dbt job's exception path and cut order: an exception message (or dbt output) with a
planted secret and an over-long text reaches every sink redacted, on one line and bounded, and the
stored transform_error is redacted before it is cut. `exception_line` is the shared helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.config.models import SourceType
from dango.platform.scheduling import jobs
from dango.transformation.failure_report import REASON_LOG_CHARS

pytestmark = pytest.mark.unit

SECRET = "sk_live_ABCDEFGHIJKLMNOP"
STORED_LIMIT = 500
COMPILE_PREFIX = "Compilation Error: "


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(f"STRIPE_KEY={SECRET}\n", encoding="utf-8", newline="\n")
    return tmp_path


class Sinks:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.broadcast: list[dict[str, Any]] = []
        self.notify: list[dict[str, Any]] = []
        self.record: list[dict[str, Any]] = []
        self.execution: list[dict[str, Any]] = []

    def texts(self) -> dict[str, list[str]]:
        log = self.root / ".dango" / "logs" / "activity.jsonl"
        rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]
        failed = [m for m in self.broadcast if m.get("event") == "dbt_failed"]
        return {
            "activity": [
                r["message"].removeprefix("Scheduled dbt failed: ")
                for r in rows
                if r["level"] == "error"
            ],
            "record": [str(r["error"]) for r in self.record],
            "execution": [str(r["error"]) for r in self.execution],
            "broadcast": [str(m["error"]) for m in failed],
            "notify": [str(n["error"]) for n in self.notify],
        }


@pytest.fixture
def sinks(project: Path, monkeypatch: pytest.MonkeyPatch) -> Sinks:
    out = Sinks(project)
    monkeypatch.setattr(jobs, "_broadcast", out.broadcast.append)
    monkeypatch.setattr(jobs, "_notify", lambda _sender, **kw: out.notify.append(kw))
    monkeypatch.setattr(
        jobs, "_try_finish_record", lambda _r, _s, _id, _fn, **kw: out.record.append(kw)
    )
    monkeypatch.setattr(jobs, "_log_execution_event", lambda **kw: out.execution.append(kw))
    return out


def _assert_safe_bounded_line(sinks: Sinks, *, limit: int = REASON_LOG_CHARS) -> None:
    texts = sinks.texts()
    for sink, values in texts.items():
        assert not any(SECRET[:8] in v for v in values), f"secret reached the {sink} sink"
    for sink, values in texts.items():
        assert len(values) == 1, (sink, values)
        assert values[0] and "\n" not in values[0], sink
        assert len(values[0]) <= limit, (sink, len(values[0]))


def _straddling_compile_error(cut: int) -> str:
    """dbt output whose reason line has the secret starting 8 characters before `cut`."""
    message = "x" * (cut - 8 - len(COMPILE_PREFIX)) + SECRET + " tail"
    return f"05:23:57  Encountered an error:\nCompilation Error\n  {message}\n"


def test_exception_message_with_a_secret_is_redacted_and_bounded_in_every_sink(
    project: Path, sinks: Sinks
) -> None:
    message = f"connection failed: password={SECRET}\nsecond line {SECRET}\n" + "y" * 5000
    with patch("dango.transformation.run_dbt_models", side_effect=RuntimeError(message)):
        jobs.run_scheduled_dbt("nightly", None, project_root=str(project))
    _assert_safe_bounded_line(sinks)
    assert sinks.texts()["broadcast"][0].startswith("connection failed")


def test_exception_secret_straddling_the_bound_is_redacted_before_the_cut(
    project: Path, sinks: Sinks
) -> None:
    message = "x" * (REASON_LOG_CHARS - 8) + SECRET + " tail"
    with patch("dango.transformation.run_dbt_models", side_effect=RuntimeError(message)):
        jobs.run_scheduled_dbt("nightly", None, project_root=str(project))
    _assert_safe_bounded_line(sinks)


def test_dbt_reason_straddling_the_bound_is_redacted_before_the_cut(
    project: Path, sinks: Sinks
) -> None:
    output = _straddling_compile_error(REASON_LOG_CHARS)
    with patch("dango.transformation.run_dbt_models", return_value=(False, output)):
        jobs.run_scheduled_dbt("nightly", None, project_root=str(project))
    _assert_safe_bounded_line(sinks)
    assert sinks.texts()["broadcast"][0].startswith(COMPILE_PREFIX)


def test_exception_line_is_one_redacted_bounded_line(project: Path) -> None:
    from dango.transformation.failure_report import exception_line

    text = exception_line(project, f"a\n\x1b[31mb\x1b[0m {SECRET}\n" + "z" * 1000)
    assert "\n" not in text and "\x1b" not in text and SECRET[:8] not in text
    assert len(text) <= REASON_LOG_CHARS and text.startswith("a b ")
    assert exception_line(project, "") == "unknown error"
    assert exception_line(project, "short") == "short"
    assert len(exception_line(project, "q" * 50, limit=20)) == 20


def test_stored_transform_error_is_redacted_before_it_is_cut(project: Path) -> None:
    from dango.ingestion import dlt_runner

    output = _straddling_compile_error(STORED_LIMIT)
    ok = {"status": "success", "source": "good", "rows_loaded": 1}
    src = MagicMock()
    src.name = "good"
    src.enabled = True
    src.type = SourceType.HUBSPOT
    with (
        patch.object(dlt_runner.DltPipelineRunner, "run_source", return_value=ok),
        patch("dango.transformation.run_dbt_models", return_value=(False, output)),
        patch("dango.transformation.generator.DbtModelGenerator"),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes", return_value=False
        ),
        patch("dango.utils.dbt_lock.DbtLock"),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value=None),
        patch.object(dlt_runner, "_connect_with_lock_retry"),
        patch.object(dlt_runner, "console"),
    ):
        summary = dlt_runner.run_sync(project, [src])
    error = summary["transform_error"]
    assert error.startswith(COMPILE_PREFIX) and len(error) <= STORED_LIMIT
    assert SECRET[:8] not in error
