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


@pytest.mark.unit
class TestDbtTelemetryEnvWiredEverywhere:
    """Review finding #1 (the blocking one): _dbt_telemetry_env() must be
    wired into every real dbt-invoking subprocess.run call, not just the
    three functions in transformation/__init__.py. Regression guard against
    a future dbt call site silently bypassing the opt-out — asserts the
    literal `env=_dbt_telemetry_env()` wiring is present at each known call
    site's source line, rather than re-deriving the list by grep (which
    would just re-check itself)."""

    _EXPECTED_SITES: tuple[tuple[str, str], ...] = (
        ("dango/transformation/__init__.py", "run_dbt_models"),
        ("dango/transformation/__init__.py", "run_dbt_snapshots"),
        ("dango/transformation/__init__.py", "generate_dbt_docs"),
        ("dango/cli/commands/transform.py", "def run("),
        ("dango/cli/commands/transform.py", "def docs("),
        ("dango/platform/local/watcher_runner.py", "def run_dbt_command("),
        ("dango/web/routes/dbt.py", "def run_dbt_model_task("),
        ("dango/cli/commands/dev.py", "def _run_dev_dbt("),
        ("dango/cli/init.py", "def _generate_dbt_docs("),
        ("dango/cli/model_wizard.py", "def _regenerate_manifest("),
        ("dango/cli/validate.py", "def _validate_dbt_models("),
    )

    def test_every_known_dbt_subprocess_site_passes_env(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for rel_path, anchor in self._EXPECTED_SITES:
            source = (repo_root / rel_path).read_text()
            assert anchor in source, f"{rel_path}: expected anchor {anchor!r} not found"
            start = source.index(anchor)
            # The function body containing this anchor must reach a
            # subprocess.run(...) call that passes env=_dbt_telemetry_env()
            # before the next top-level def/class (a rough but effective
            # "same function" boundary for these small, single-call functions).
            next_def = source.find("\ndef ", start + 1)
            next_top_level = source.find("\n\ndef ", start + 1)
            end = min(x for x in (next_def, next_top_level, len(source)) if x != -1)
            body = source[start:end]
            assert "env=_dbt_telemetry_env()" in body, (
                f"{rel_path} ({anchor}): subprocess.run call in this function "
                f"does not pass env=_dbt_telemetry_env() — dbt telemetry "
                f"opt-out would silently not apply here"
            )
