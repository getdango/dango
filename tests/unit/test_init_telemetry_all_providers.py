"""tests/unit/test_init_telemetry_all_providers.py

The `dango init` telemetry prompt is all-or-nothing across Dango, dbt, dlt and Metabase.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import dango.telemetry as telemetry
from dango.cli.init import ProjectInitializer, _ask_telemetry_consent
from dango.config.models import DangoConfig, DataSource, ProjectContext, SourcesConfig, SourceType
from dango.visualization.metabase import get_metabase_telemetry_state


@pytest.fixture(autouse=True)
def _isolated_dango_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "home" / ".dango"
    monkeypatch.setattr(telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in ("DO_NOT_TRACK", "DANGO_TELEMETRY", *telemetry._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


def _config() -> DangoConfig:
    return DangoConfig(
        project=ProjectContext(name="T", created_by="t", purpose="t"),
        sources=SourcesConfig(sources=[DataSource(name="c", type=SourceType.CSV)]),
    )


def _states(project: Path) -> dict[str, bool]:
    """Read each provider back through the getters `dango telemetry status` uses."""
    return {
        "dango": telemetry.is_telemetry_enabled(),
        "dbt": telemetry.get_dbt_telemetry_state(),
        "dlt": telemetry.get_dlt_telemetry_state(project),
        "metabase": get_metabase_telemetry_state(project),
    }


def _run_prompt(project: Path, answer: bool | None) -> Any:
    with (
        patch("dango.cli.init._ask_telemetry_consent", return_value=answer),
        patch("dango.telemetry.ping") as ping,
    ):
        ProjectInitializer(project)._prompt_telemetry_consent(_config())
    return ping


@pytest.mark.unit
class TestInitTelemetryAllProviders:
    def test_no_turns_every_provider_off(self, tmp_path: Path) -> None:
        ping = _run_prompt(tmp_path, False)
        assert _states(tmp_path) == {"dango": False, "dbt": False, "dlt": False, "metabase": False}
        ping.assert_not_called()

    def test_yes_turns_every_provider_on_and_pings(self, tmp_path: Path) -> None:
        ping = _run_prompt(tmp_path, True)
        assert _states(tmp_path) == {"dango": True, "dbt": True, "dlt": True, "metabase": True}
        ping.assert_called_once_with("install", source_types=["csv"])

    def test_no_answer_persists_nothing(self, tmp_path: Path) -> None:
        _run_prompt(tmp_path, None)
        assert telemetry.has_recorded_consent() is False
        assert not telemetry._GLOBAL_CONFIG_FILE.exists()
        assert not (tmp_path / ".dango" / "metabase_telemetry_state").exists()

    def test_metabase_provisioning_follows_recorded_answer(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import get_metabase_telemetry_intent

        assert get_metabase_telemetry_intent(tmp_path) is False
        _run_prompt(tmp_path, True)
        assert get_metabase_telemetry_intent(tmp_path) is True

    def test_prompt_text_names_every_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake_prompt(text: str) -> str:
            seen.append(text)
            return "no"

        monkeypatch.setattr("click.prompt", fake_prompt)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert _ask_telemetry_consent() is False
        text = seen[0]
        for needle in ("- Dango:", "- dbt:", "- dlt:", "- Metabase:", "Allow all of the above?"):
            assert needle in text
        assert text.endswith("(yes/no)   You can change this any time: dango telemetry on|off --all")
