"""tests/unit/test_telemetry_unified.py

Tests for dango.cli.commands.telemetry (unified `dango telemetry`
status/on/off) and the dbt subprocess telemetry-env helper it drives in
dango.transformation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import dango.telemetry as dango_telemetry
from dango.cli.main import cli
from dango.transformation import _dbt_telemetry_env


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect Path.home() (used by the dbt sentinel file and dango.telemetry's
    module-level path constants) into tmp_path, and clear opt-out env vars so
    tests are hermetic regardless of the host machine's real ~/.dango state.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config_dir = tmp_path / ".dango"
    monkeypatch.setattr(dango_telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(dango_telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(dango_telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in ("DO_NOT_TRACK", "DANGO_TELEMETRY", *dango_telemetry._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.unit
class TestDbtTelemetryEnv:
    """_dbt_telemetry_env() — the helper wired into all three dbt subprocess.run calls."""

    def test_dbt_env_includes_opt_out(self, tmp_path: Path) -> None:
        """When the sentinel file says 'false', the env carries the real
        dbt-core opt-out var (DBT_SEND_ANONYMOUS_USAGE_STATS — verified
        against installed dbt-core in test_egress_allowlist.py) plus
        DO_NOT_TRACK as defense in depth."""
        sentinel = tmp_path / ".dango" / "dbt_telemetry"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("false")

        env = _dbt_telemetry_env()

        assert env["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
        assert env["DO_NOT_TRACK"] == "1"

    def test_dbt_env_default_clean(self, tmp_path: Path) -> None:
        """When no sentinel file exists, the env is an unmodified copy of
        os.environ — no telemetry opt-out vars injected."""
        env = _dbt_telemetry_env()

        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in env
        assert "DO_NOT_TRACK" not in env

    def test_dbt_env_is_a_copy_not_the_real_environ(self, tmp_path: Path) -> None:
        """Regression risk from the task spec: never mutate os.environ in place."""
        import os

        sentinel = tmp_path / ".dango" / "dbt_telemetry"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("false")

        env = _dbt_telemetry_env()

        assert env is not os.environ
        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in os.environ
        assert "DO_NOT_TRACK" not in os.environ


@pytest.mark.unit
class TestDltWriteThrough:
    """_set_dlt_telemetry / _get_dlt_telemetry_state — .dlt/config.toml write-through."""

    def test_dlt_write_through(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _set_dlt_telemetry

        _set_dlt_telemetry(False, tmp_path)

        config_path = tmp_path / ".dlt" / "config.toml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "dlthub_telemetry" in content
        # Written as a native TOML boolean (unquoted), matching the type of
        # dlt's own dlthub_telemetry field (RuntimeConfiguration.dlthub_telemetry: bool)
        # and the value type dlt's own CLI writes via WritableConfigValue(..., bool, ...).
        assert "false" in content
        assert '"false"' not in content

    def test_dlt_read_state_off(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_dlt_telemetry_state, _set_dlt_telemetry

        _set_dlt_telemetry(False, tmp_path)

        assert _get_dlt_telemetry_state(tmp_path) is False

    def test_dlt_read_state_default_on(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_dlt_telemetry_state

        assert _get_dlt_telemetry_state(tmp_path) is True

    def test_dlt_round_trip_on(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_dlt_telemetry_state, _set_dlt_telemetry

        _set_dlt_telemetry(False, tmp_path)
        _set_dlt_telemetry(True, tmp_path)

        assert _get_dlt_telemetry_state(tmp_path) is True


@pytest.mark.unit
class TestTelemetryStatusCommand:
    def test_telemetry_status_runs(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "status"])

        assert result.exit_code == 0, result.output
        assert "dango" in result.output
        assert "dbt-core" in result.output
        assert "dlt" in result.output
        assert "metabase" in result.output
        # The absolute wording constraint (D-telemetry-unified.md): never
        # claim "nothing leaves your machine".
        assert "nothing leaves your machine" not in result.output.lower()


@pytest.mark.unit
class TestTelemetryOffAllOutsideProject:
    """Regression risk + acceptance criterion: `--all` outside a project must
    succeed for dango+dbt and warn (not crash) for dlt+metabase."""

    def test_off_all_outside_project_warns_not_crashes(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "off", "--all"])

        assert result.exit_code == 0, result.output
        assert "dango: telemetry disabled" in result.output
        assert "dbt: telemetry disabled" in result.output
        assert "dlt" in result.output and "skipped" in result.output
        assert "metabase" in result.output and "skipped" in result.output

    def test_off_provider_dlt_outside_project_raises(self, tmp_path: Path) -> None:
        """A single explicit --provider request outside a project is a hard
        error, not a silent skip — this is a deliberate action, not a sweep."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "off", "--provider", "dlt"])

        assert result.exit_code != 0
        assert "Dango project" in result.output


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
