"""tests/unit/test_cli_init_telemetry.py

Tests for the telemetry consent prompt wired into
ProjectInitializer.initialize() / _prompt_telemetry_consent().
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import dango.telemetry as telemetry
from dango.cli.init import ProjectInitializer
from dango.config.models import (
    DangoConfig,
    DataSource,
    ProjectContext,
    SourcesConfig,
    SourceType,
)


@pytest.fixture(autouse=True)
def _isolated_dango_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate telemetry.py's persisted state from the real developer machine."""
    config_dir = tmp_path / "home" / ".dango"
    monkeypatch.setattr(telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in ("DO_NOT_TRACK", "DANGO_TELEMETRY", *telemetry._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


def _make_config() -> DangoConfig:
    """Build a minimal DangoConfig with two enabled sources and one disabled one."""
    return DangoConfig(
        project=ProjectContext(name="Test Project", created_by="tester", purpose="testing"),
        sources=SourcesConfig(
            sources=[
                DataSource(name="my_csv", type=SourceType.CSV),
                DataSource(name="my_sheet", type=SourceType.GOOGLE_SHEETS),
                DataSource(name="paused_source", type=SourceType.STRIPE, enabled=False),
            ]
        ),
    )


@pytest.mark.unit
class TestPromptTelemetryConsent:
    """Tests for ProjectInitializer._prompt_telemetry_consent()."""

    def test_skips_when_ci(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No prompt fires when running in a detected CI environment."""
        monkeypatch.setenv("CI", "true")
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init.safe_confirm") as mock_confirm:
            initializer._prompt_telemetry_consent(_make_config())
        mock_confirm.assert_not_called()

    def test_skips_when_already_answered(self, tmp_path: Path) -> None:
        """No prompt fires if the user has already answered once before."""
        telemetry.set_telemetry_enabled(True)
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init.safe_confirm") as mock_confirm:
            initializer._prompt_telemetry_consent(_make_config())
        mock_confirm.assert_not_called()

    def test_skips_when_do_not_track_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prompt fires when DO_NOT_TRACK=1 is set."""
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init.safe_confirm") as mock_confirm:
            initializer._prompt_telemetry_consent(_make_config())
        mock_confirm.assert_not_called()

    def test_prompts_and_persists_yes(self, tmp_path: Path) -> None:
        """Answering yes persists consent and fires the install ping with enabled source types only."""
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("dango.cli.init.safe_confirm", return_value=True) as mock_confirm,
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

        mock_confirm.assert_called_once()
        # The disabled "paused_source" (stripe) must not appear.
        mock_ping.assert_called_once_with("install", source_types=["csv", "google_sheets"])
        assert telemetry.is_telemetry_enabled() is True
        assert telemetry.has_recorded_consent() is True

    def test_prompts_and_persists_no_skips_ping(self, tmp_path: Path) -> None:
        """Answering no persists the opt-out and never fires a ping."""
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("dango.cli.init.safe_confirm", return_value=False),
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

        mock_ping.assert_not_called()
        assert telemetry.is_telemetry_enabled() is False
        assert telemetry.has_recorded_consent() is True

    def test_skips_and_never_persists_when_not_a_tty(self, tmp_path: Path) -> None:
        """A non-interactive session (no TTY) is never asked, and nothing is recorded.

        Regression test: previously, safe_confirm()'s non-interactive
        fallback (default=False) was persisted as if it were a real
        answer, permanently locking the machine into opt-out without the
        user ever seeing the prompt.
        """
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("dango.cli.init.safe_confirm") as mock_confirm,
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

        mock_confirm.assert_not_called()
        mock_ping.assert_not_called()
        assert telemetry.has_recorded_consent() is False


@pytest.mark.unit
class TestInitializeSkipsPromptWhenSkipWizard:
    """Covers the skip_wizard gate that lives in initialize(), not the helper."""

    def test_skip_wizard_never_prompts(self, tmp_path: Path) -> None:
        """A --skip-wizard blank-project init never triggers the telemetry prompt."""
        project_dir = tmp_path / "proj"
        initializer = ProjectInitializer(project_dir)
        with (
            patch.object(ProjectInitializer, "_prompt_telemetry_consent") as mock_prompt,
            patch.object(ProjectInitializer, "_setup_metabase", return_value=True),
            patch.object(ProjectInitializer, "_generate_dbt_docs", return_value=True),
            patch.object(ProjectInitializer, "_setup_auth", return_value=True),
        ):
            initializer.initialize(skip_wizard=True)

        mock_prompt.assert_not_called()
