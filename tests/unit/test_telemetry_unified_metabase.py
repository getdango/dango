"""tests/unit/test_telemetry_unified_metabase.py

Metabase-specific tests for the unified `dango telemetry` command
(dango.visualization.metabase.set_metabase_telemetry and
dango.cli.commands.telemetry's Metabase provider helpers). Split out of
tests/unit/test_telemetry_unified.py to stay under the 500-line file-size
limit (scripts/check_file_sizes.py) — dbt/dlt and top-level CLI tests
remain there.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import dango.telemetry as dango_telemetry
from dango.cli.main import cli


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect Path.home() and dango.telemetry's module-level path
    constants into tmp_path, and clear opt-out env vars, so tests are
    hermetic regardless of the host machine's real ~/.dango state.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / ".dango"
    monkeypatch.setattr(dango_telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(dango_telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(dango_telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in ("DO_NOT_TRACK", "DANGO_TELEMETRY", *dango_telemetry._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.unit
class TestMetabaseTelemetry:
    """set_metabase_telemetry — request-level behavior, mocked (no live Metabase)."""

    def test_set_metabase_telemetry_calls_correct_endpoint(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            set_metabase_telemetry(tmp_path, False, metabase_url="http://localhost:3000")

        put_args = mock_session.put.call_args
        assert put_args.args[0] == "http://localhost:3000/api/setting/anon-tracking-enabled"
        assert put_args.kwargs["json"] == {"value": False}
        assert put_args.kwargs["headers"]["X-Metabase-Session"] == "sess-123"

    def test_set_metabase_telemetry_wraps_connection_failure(self, tmp_path: Path) -> None:
        """Regression risk from the task spec: must not raise a raw requests
        exception if Metabase is not running — surface a helpful ClickException."""
        import click
        import requests

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        from dango.visualization.metabase import set_metabase_telemetry

        with patch(
            "dango.visualization.metabase.requests.Session",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with pytest.raises(click.ClickException):
                set_metabase_telemetry(tmp_path, False)

    def test_set_metabase_telemetry_login_401_gives_credentials_message(
        self, tmp_path: Path
    ) -> None:
        """Review fix #2: a 401/403 on login (stale admin password) must NOT
        be mislabeled "is it running?" — Metabase is reachable and running
        fine, the credentials are just wrong. HTTPError from
        raise_for_status() is a RequestException subclass, so without the
        explicit status-code check this fell into the generic
        "is it running?" branch."""
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: stale-password\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.status_code = 401

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            with pytest.raises(Exception) as exc_info:
                set_metabase_telemetry(tmp_path, False)

        message = str(exc_info.value)
        assert "credentials" in message.lower()
        assert "is it running" not in message.lower()

    def test_set_metabase_telemetry_reads_stored_metabase_url(self, tmp_path: Path) -> None:
        """Review fix #3: when metabase_url isn't passed explicitly, read it
        from the "metabase_url" key in .dango/metabase.yml (matching the
        precedent in cli/commands/metabase_cmd.py:343), instead of always
        hardcoding localhost:3000."""
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
            "metabase_url: http://metabase.internal:9000\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            set_metabase_telemetry(tmp_path, False)

        post_url = mock_session.post.call_args.args[0]
        put_url = mock_session.put.call_args.args[0]
        assert post_url == "http://metabase.internal:9000/api/session"
        assert put_url == "http://metabase.internal:9000/api/setting/anon-tracking-enabled"

    def test_set_metabase_telemetry_falls_back_to_localhost_when_url_key_absent(
        self, tmp_path: Path
    ) -> None:
        """No "metabase_url" key in metabase.yml and no explicit override ->
        still falls back to the documented default, doesn't crash."""
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            set_metabase_telemetry(tmp_path, False)

        assert mock_session.put.call_args.args[0] == (
            "http://localhost:3000/api/setting/anon-tracking-enabled"
        )

    def test_set_metabase_telemetry_writes_state_cache_on_success(self, tmp_path: Path) -> None:
        """Review fix #2: a successful call writes a local last-known-state
        cache that _get_metabase_telemetry_state() reads, instead of status
        being hardcoded."""
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            set_metabase_telemetry(tmp_path, False)

        state_file = tmp_path / ".dango" / "metabase_telemetry_state"
        assert state_file.read_text().strip() == "false"

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            set_metabase_telemetry(tmp_path, True)

        assert state_file.read_text().strip() == "true"

    def test_set_metabase_telemetry_cache_write_failure_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """The failure-ordering bug found in review: if the real Metabase API
        call already succeeded, a failure writing the secondary local status
        cache must NOT be reported as a command failure — it's a "status may
        be stale" problem, not an "operation failed" problem. Forces a real
        write failure (state-file path collides with an existing directory,
        so write_text() raises IsADirectoryError) rather than mocking the
        exception, so this exercises the actual except-block wiring."""
        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )
        # Pre-create the cache path AS A DIRECTORY so write_text() fails.
        (creds_dir / "metabase_telemetry_state").mkdir()

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            # Must return normally — no exception — even though the cache
            # write underneath will fail.
            set_metabase_telemetry(tmp_path, False)

        # The real API call still happened correctly.
        assert mock_session.put.call_args.args[0] == (
            "http://localhost:3000/api/setting/anon-tracking-enabled"
        )
        assert mock_session.put.call_args.kwargs["json"] == {"value": False}

    def test_set_metabase_telemetry_wraps_malformed_yaml(self, tmp_path: Path) -> None:
        """Review fix #4: a broad except Exception fallback converts anything
        else in the credentials/login/API flow (e.g. yaml.YAMLError from a
        hand-edited metabase.yml) into the same clean click.ClickException
        contract, instead of letting a raw traceback through."""
        import click

        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        # Invalid YAML (unbalanced flow mapping) -> yaml.safe_load raises YAMLError.
        (creds_dir / "metabase.yml").write_text("admin: [email: broken\n")

        with pytest.raises(click.ClickException):
            set_metabase_telemetry(tmp_path, False)

    def test_set_metabase_telemetry_wraps_non_json_login_response(self, tmp_path: Path) -> None:
        """Same broad-except contract for a 200 login response with a
        non-JSON body (login_response.json() raising ValueError)."""
        import click

        from dango.visualization.metabase import set_metabase_telemetry

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.post.return_value.json.side_effect = ValueError("not JSON")

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            with pytest.raises(click.ClickException):
                set_metabase_telemetry(tmp_path, False)


@pytest.mark.unit
class TestMetabaseTelemetryStatus:
    """_get_metabase_telemetry_state — review fix #2: status must reflect the
    real last-set state (via the cache file), not a hardcoded True."""

    def test_defaults_true_when_no_project(self) -> None:
        from dango.cli.commands.telemetry import _get_metabase_telemetry_state

        assert _get_metabase_telemetry_state(None) is True

    def test_defaults_true_when_no_cache_file(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_metabase_telemetry_state

        assert _get_metabase_telemetry_state(tmp_path) is True

    def test_reads_off_from_cache_file(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_metabase_telemetry_state

        state_dir = tmp_path / ".dango"
        state_dir.mkdir()
        (state_dir / "metabase_telemetry_state").write_text("false")

        assert _get_metabase_telemetry_state(tmp_path) is False

    def test_reads_on_from_cache_file(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_metabase_telemetry_state

        state_dir = tmp_path / ".dango"
        state_dir.mkdir()
        (state_dir / "metabase_telemetry_state").write_text("true")

        assert _get_metabase_telemetry_state(tmp_path) is True

    def test_status_reflects_real_toggle_end_to_end(self, tmp_path: Path) -> None:
        """The specific bug three review angles independently flagged: status
        must change after a real successful `telemetry off --provider
        metabase` call, not stay stuck on "on" forever."""
        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        (creds_dir / "metabase.yml").write_text(
            "admin:\n  email: admin@example.com\n  password: secret123\n"
        )

        mock_session = MagicMock()
        mock_session.post.return_value.json.return_value = {"id": "sess-123"}
        mock_session.post.return_value.raise_for_status.return_value = None
        mock_session.put.return_value.raise_for_status.return_value = None

        from dango.cli.commands.telemetry import (
            _get_metabase_telemetry_state,
            _set_metabase_telemetry,
        )

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            assert _get_metabase_telemetry_state(tmp_path) is True
            _set_metabase_telemetry(False, tmp_path)
            assert _get_metabase_telemetry_state(tmp_path) is False


@pytest.mark.unit
class TestMetabaseNotConfiguredCheckSingleSource:
    """Review fix #10: the duplicate `creds_file.exists()` check was removed
    from telemetry.py's wrapper — the single remaining check, in
    metabase.py's set_metabase_telemetry(), must still surface the same
    user-facing error end-to-end."""

    def test_off_provider_metabase_not_configured_inside_project(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "project"
        (project_dir / ".dango").mkdir(parents=True)
        (project_dir / ".dango" / "project.yml").write_text("name: test\n")
        # Deliberately no .dango/metabase.yml.

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=project_dir):
            result = runner.invoke(cli, ["telemetry", "off", "--provider", "metabase"])

        assert result.exit_code != 0
        assert "Metabase not configured" in result.output
