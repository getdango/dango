"""tests/unit/test_telemetry.py

Tests for dango.telemetry: opt-out precedence, UUID persistence, and
the outgoing ping payload shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dango.telemetry as telemetry


@pytest.fixture(autouse=True)
def _isolated_dango_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect telemetry.py's module-level path constants into tmp_path.

    Patching Path.home() alone would not work here since _CONFIG_DIR,
    _IDENTITY_FILE, and _GLOBAL_CONFIG_FILE are already bound at import
    time, so the module attributes are patched directly instead.
    """
    config_dir = tmp_path / ".dango"
    monkeypatch.setattr(telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in ("DO_NOT_TRACK", "DANGO_TELEMETRY", *telemetry._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.unit
class TestIsCi:
    """Tests for telemetry.is_ci()."""

    def test_false_with_no_env_vars(self) -> None:
        """No CI env vars set means is_ci() is False."""
        assert telemetry.is_ci() is False

    @pytest.mark.parametrize("var", telemetry._CI_ENV_VARS)
    def test_true_when_ci_env_var_set(self, var: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Any recognized CI env var being set makes is_ci() True."""
        monkeypatch.setenv(var, "1")
        assert telemetry.is_ci() is True


@pytest.mark.unit
class TestIsTelemetryEnabled:
    """Tests for telemetry.is_telemetry_enabled()."""

    def test_true_by_default(self) -> None:
        """No opt-out signal present means telemetry is enabled."""
        assert telemetry.is_telemetry_enabled() is True

    def test_do_not_track_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DO_NOT_TRACK=1 disables telemetry."""
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        assert telemetry.is_telemetry_enabled() is False

    def test_dango_telemetry_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DANGO_TELEMETRY=0 disables telemetry."""
        monkeypatch.setenv("DANGO_TELEMETRY", "0")
        assert telemetry.is_telemetry_enabled() is False

    def test_ci_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Running in CI disables telemetry regardless of stored consent."""
        monkeypatch.setenv("CI", "true")
        telemetry.set_telemetry_enabled(True)
        assert telemetry.is_telemetry_enabled() is False

    def test_config_yml_opt_out(self) -> None:
        """telemetry: false in ~/.dango/config.yml disables telemetry."""
        telemetry._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        telemetry._GLOBAL_CONFIG_FILE.write_text("telemetry: false\n")
        assert telemetry.is_telemetry_enabled() is False

    def test_config_yml_opt_in_is_not_required(self) -> None:
        """Absence of the config.yml key doesn't disable telemetry on its own."""
        telemetry._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        telemetry._GLOBAL_CONFIG_FILE.write_text("telemetry: true\n")
        assert telemetry.is_telemetry_enabled() is True

    def test_stored_consent_no_disables(self) -> None:
        """A stored {"enabled": false} answer disables telemetry."""
        telemetry.set_telemetry_enabled(False)
        assert telemetry.is_telemetry_enabled() is False

    def test_stored_consent_yes_enables(self) -> None:
        """A stored {"enabled": true} answer keeps telemetry enabled."""
        telemetry.set_telemetry_enabled(True)
        assert telemetry.is_telemetry_enabled() is True

    def test_corrupt_identity_file_falls_through(self) -> None:
        """A corrupt telemetry.json is treated as no opt-out, not an error."""
        telemetry._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        telemetry._IDENTITY_FILE.write_text("not valid json{{{")
        assert telemetry.is_telemetry_enabled() is True

    def test_corrupt_config_yml_falls_through(self) -> None:
        """Corrupt YAML in config.yml is treated as no opt-out, not an error."""
        telemetry._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        telemetry._GLOBAL_CONFIG_FILE.write_text(": : : not valid yaml")
        assert telemetry.is_telemetry_enabled() is True


@pytest.mark.unit
class TestHasRecordedConsent:
    """Tests for telemetry.has_recorded_consent()."""

    def test_false_when_no_identity_file(self) -> None:
        """No telemetry.json means consent has never been recorded."""
        assert telemetry.has_recorded_consent() is False

    def test_true_after_set_telemetry_enabled(self) -> None:
        """set_telemetry_enabled() records an answer either way."""
        telemetry.set_telemetry_enabled(False)
        assert telemetry.has_recorded_consent() is True

    def test_false_when_uuid_present_but_no_enabled_key(self) -> None:
        """A UUID alone (no consent answer yet) doesn't count as recorded."""
        telemetry._get_or_create_uuid()
        assert telemetry.has_recorded_consent() is False


@pytest.mark.unit
class TestGetOrCreateUuid:
    """Tests for telemetry._get_or_create_uuid()."""

    def test_persists_across_calls(self) -> None:
        """The same UUID is returned on every subsequent call."""
        first = telemetry._get_or_create_uuid()
        second = telemetry._get_or_create_uuid()
        assert first == second

    def test_stored_on_disk(self) -> None:
        """The UUID is written to the machine-level identity file."""
        generated = telemetry._get_or_create_uuid()
        data = json.loads(telemetry._IDENTITY_FILE.read_text())
        assert data["uuid"] == generated

    def test_recovers_from_corrupt_file(self) -> None:
        """A corrupt telemetry.json doesn't raise — a fresh UUID is generated."""
        telemetry._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        telemetry._IDENTITY_FILE.write_text("not valid json{{{")
        result = telemetry._get_or_create_uuid()
        assert result


@pytest.mark.unit
class TestSetTelemetryEnabled:
    """Tests for telemetry.set_telemetry_enabled()."""

    def test_merges_without_clobbering_uuid(self) -> None:
        """Setting consent doesn't erase an already-stored UUID."""
        existing_uuid = telemetry._get_or_create_uuid()
        telemetry.set_telemetry_enabled(False)
        data = json.loads(telemetry._IDENTITY_FILE.read_text())
        assert data["uuid"] == existing_uuid
        assert data["enabled"] is False


@pytest.mark.unit
class TestPing:
    """Tests for telemetry.ping()."""

    def test_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ping() never opens a connection when telemetry is disabled."""
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        with patch("urllib.request.urlopen") as mock_urlopen:
            telemetry.ping("install")
        mock_urlopen.assert_not_called()

    def test_silent_on_network_failure(self) -> None:
        """A network failure inside ping() never propagates."""
        telemetry.set_telemetry_enabled(True)
        with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
            telemetry.ping("install")  # must not raise

    def test_sends_expected_payload_shape(self) -> None:
        """The outgoing payload has exactly the fields the deployed Worker expects."""
        telemetry.set_telemetry_enabled(True)
        mock_response = Mock()
        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            telemetry.ping("install", source_types=["postgres", "stripe"])

        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        assert set(body.keys()) == {
            "uuid",
            "event",
            "version",
            "os",
            "python_version",
            "source_types",
            "is_ci",
        }
        assert body["event"] == "install"
        assert body["source_types"] == ["postgres", "stripe"]
        assert body["is_ci"] is False
        assert request.full_url == telemetry.TELEMETRY_ENDPOINT
        # Cloudflare's bot-fight mode on this endpoint blocks requests with
        # no User-Agent (urllib sends none by default) — regression test
        # for a live 403 caught during manual verification.
        assert request.get_header("User-agent")
