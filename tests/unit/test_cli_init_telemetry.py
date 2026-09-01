"""tests/unit/test_cli_init_telemetry.py

Tests for the telemetry consent prompt wired into
ProjectInitializer.initialize() / _prompt_telemetry_consent(), and the
_ask_telemetry_consent() helper it uses instead of safe_confirm().
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

import dango.telemetry as telemetry
from dango.cli.init import ProjectInitializer, _ask_telemetry_consent
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
class TestAskTelemetryConsent:
    """Tests for the module-level _ask_telemetry_consent() helper.

    Uses a real (unmocked) sys.stdin swapped for an in-memory stream
    rather than mocking click.prompt — this proves the function reads
    genuinely piped data correctly, not just that a mock returns what
    we told it to.
    """

    def test_honors_a_real_yes_answer_even_though_stdin_is_not_a_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real 'yes' piped into stdin is read and honoured.

        Regression test: an earlier design pre-checked
        sys.stdin.isatty() and returned early for ANY non-TTY stdin,
        silently discarding a real piped answer just because piped
        input is never a TTY — even though the data was genuinely
        there to read. This is the exact scenario `echo yes | dango
        init` hits.
        """
        piped_stdin = io.StringIO("yes\n")
        assert piped_stdin.isatty() is False  # genuinely non-interactive, but has real data
        monkeypatch.setattr("sys.stdin", piped_stdin)
        assert _ask_telemetry_consent() is True

    def test_honors_a_real_no_answer_via_pipe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real 'no' piped into stdin is read and honoured."""
        monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))
        assert _ask_telemetry_consent() is False

    def test_returns_none_on_genuine_eof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty stdin (no data at all) returns None, not a substituted default."""
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert _ask_telemetry_consent() is None

    def test_returns_none_when_stdin_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A closed fd 0 (sys.stdin is None) returns None instead of crashing.

        Regression test: CPython sets sys.stdin to None when fd 0 is
        closed entirely (e.g. a daemonized invocation) rather than
        merely redirected; input() then raises RuntimeError, which an
        earlier, narrower except clause did not catch.
        """
        monkeypatch.setattr("sys.stdin", None)
        assert _ask_telemetry_consent() is None

    def test_reprompts_on_invalid_input_then_accepts_valid_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognized answer re-prompts instead of guessing or crashing."""
        monkeypatch.setattr("sys.stdin", io.StringIO("maybe\nyes\n"))
        assert _ask_telemetry_consent() is True


@pytest.mark.unit
class TestPromptTelemetryConsent:
    """Tests for ProjectInitializer._prompt_telemetry_consent()."""

    def test_skips_when_ci(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No prompt fires when running in a detected CI environment."""
        monkeypatch.setenv("CI", "true")
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init._ask_telemetry_consent") as mock_ask:
            initializer._prompt_telemetry_consent(_make_config())
        mock_ask.assert_not_called()

    def test_skips_when_already_answered(self, tmp_path: Path) -> None:
        """No prompt fires if the user has already answered once before."""
        telemetry.set_telemetry_enabled(True)
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init._ask_telemetry_consent") as mock_ask:
            initializer._prompt_telemetry_consent(_make_config())
        mock_ask.assert_not_called()

    def test_skips_when_do_not_track_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prompt fires when DO_NOT_TRACK=1 is set."""
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        initializer = ProjectInitializer(tmp_path)
        with patch("dango.cli.init._ask_telemetry_consent") as mock_ask:
            initializer._prompt_telemetry_consent(_make_config())
        mock_ask.assert_not_called()

    def test_prompts_and_persists_yes(self, tmp_path: Path) -> None:
        """Answering yes persists consent and fires the install ping with enabled source types only."""
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("dango.cli.init._ask_telemetry_consent", return_value=True) as mock_ask,
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

        mock_ask.assert_called_once()
        # The disabled "paused_source" (stripe) must not appear.
        mock_ping.assert_called_once_with("install", source_types=["csv", "google_sheets"])
        assert telemetry.is_telemetry_enabled() is True
        assert telemetry.has_recorded_consent() is True

    def test_prompts_and_persists_no_skips_ping(self, tmp_path: Path) -> None:
        """Answering no persists the opt-out and never fires a ping."""
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("dango.cli.init._ask_telemetry_consent", return_value=False),
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

        mock_ping.assert_not_called()
        assert telemetry.is_telemetry_enabled() is False
        assert telemetry.has_recorded_consent() is True

    def test_no_real_answer_skips_without_persisting_or_pinging(self, tmp_path: Path) -> None:
        """When _ask_telemetry_consent() returns None (no real answer obtained), nothing is recorded.

        Regression test: previously, a non-TTY session's unseen fallback
        answer was persisted as if it were a real "no", permanently
        locking the machine into opt-out without the user ever seeing
        the prompt.
        """
        initializer = ProjectInitializer(tmp_path)
        with (
            patch("dango.cli.init._ask_telemetry_consent", return_value=None),
            patch("dango.telemetry.ping") as mock_ping,
        ):
            initializer._prompt_telemetry_consent(_make_config())

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
