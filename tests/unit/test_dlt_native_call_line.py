"""tests/unit/test_dlt_native_call_line.py

`dlt_native` announces which source function it calls. That line shows only the argument names: the
values (API keys, tokens, nested settings) never reach the terminal or the sync logs.
"""

from __future__ import annotations

import importlib
import types
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest

from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner

pytestmark = pytest.mark.unit

SECRETS = ["example-key-0123456789abcdef-not-real", "nested-token-value", "list-secret-1", "tiny"]


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def run_with(tmp_path: Any, function_kwargs: dict[str, Any]) -> list[str]:
    """Everything `_run_dlt_native_source` printed for one real sync with these kwargs."""

    @dlt.resource(name="events", write_disposition="append")
    def events() -> Any:
        yield {"id": 1}

    @dlt.source(name="calls")
    def make(**kwargs: Any) -> Any:
        return events

    config = DataSource(
        name="calls",
        type=SourceType.DLT_NATIVE,
        dlt_native=DltNativeConfig(
            source_module="fixture_module", source_function="make", function_kwargs=function_kwargs
        ),
    )
    module = types.SimpleNamespace(make=make)
    real_import = importlib.import_module

    def _import(name: str, *args: Any, **kwargs: Any) -> Any:
        return module if name == "fixture_module" else real_import(name, *args, **kwargs)

    printed = MagicMock()
    with (
        patch("dango.ingestion.dlt_runner.console", printed),
        patch("dango.ingestion.dlt_runner.importlib.import_module", side_effect=_import),
    ):
        DltPipelineRunner(tmp_path)._run_dlt_native_source(config)
    return [" ".join(str(a) for a in c.args) for c in printed.print.call_args_list]


def test_the_call_line_has_names_and_no_values(tmp_path: Any) -> None:
    lines = run_with(
        tmp_path,
        {
            "path": "data/events.json",
            "api_key": SECRETS[0],
            "options": {"token": SECRETS[1], "ids": [SECRETS[2]]},
            "flag": SECRETS[3],
        },
    )
    output = "\n".join(lines)
    for secret in SECRETS:
        assert secret not in output
    assert "data/events.json" not in output
    call = next(line for line in lines if "Calling make" in line)
    for name in ("path", "api_key", "options", "flag"):
        assert name in call


def test_a_call_without_arguments_still_names_the_function(tmp_path: Any) -> None:
    lines = run_with(tmp_path, {})
    assert any("Calling make" in line for line in lines)
