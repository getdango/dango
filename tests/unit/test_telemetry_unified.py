"""tests/unit/test_telemetry_unified.py

Tests for dango.cli.commands.telemetry (unified `dango telemetry`
status/on/off) covering the dbt and dlt providers, the dbt subprocess
telemetry-env helper in dango.transformation, and the top-level status/
--all CLI behavior. Metabase-specific tests live in
tests/unit/test_telemetry_unified_metabase.py (split out to stay under the
500-line file-size limit — see scripts/check_file_sizes.py).

1.0.8-U relocated the dbt/dlt provider state read/write logic out of
`dango.cli.commands.telemetry`'s private functions and into
`dango.telemetry` (Level 0), so `web/routes/telemetry.py` (Level 2) can
call it without a Level-2-imports-Level-3 violation. The CLI-facing tests
above are unchanged (they still import `_get_dbt_telemetry_state` etc. from
`dango.cli.commands.telemetry`, now thin wrappers) — the
`TestLevel0DbtDltTelemetryFunctions` class below tests the relocated
functions directly at their new home.
"""

from __future__ import annotations

from pathlib import Path

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
        """When config.yml's dbt_telemetry key is False, the env carries the
        real dbt-core opt-out var (DBT_SEND_ANONYMOUS_USAGE_STATS — verified
        against installed dbt-core in test_egress_allowlist.py) plus
        DO_NOT_TRACK as defense in depth."""
        from dango.telemetry import _write_global_config_key

        _write_global_config_key("dbt_telemetry", False)

        env = _dbt_telemetry_env()

        assert env["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
        assert env["DO_NOT_TRACK"] == "1"

    def test_dbt_env_default_clean(self, tmp_path: Path) -> None:
        """When config.yml has no dbt_telemetry key, the env is an
        unmodified copy of os.environ — no telemetry opt-out vars injected."""
        env = _dbt_telemetry_env()

        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in env
        assert "DO_NOT_TRACK" not in env

    def test_dbt_env_is_a_copy_not_the_real_environ(self, tmp_path: Path) -> None:
        """Regression risk from the task spec: never mutate os.environ in place."""
        import os

        from dango.telemetry import _write_global_config_key

        _write_global_config_key("dbt_telemetry", False)

        env = _dbt_telemetry_env()

        assert env is not os.environ
        assert "DBT_SEND_ANONYMOUS_USAGE_STATS" not in os.environ
        assert "DO_NOT_TRACK" not in os.environ


@pytest.mark.unit
class TestDltWriteThrough:
    """_set_dlt_telemetry / _get_dlt_telemetry_state — ~/.dango/config.yml
    write-through (1.0.8-OPS-2: moved off project-level .dlt/config.toml,
    which was git-tracked by default — see
    v1.0.x-planning/1.0.8/OPS-2-telemetry-storage-consolidation.md).
    `project_root` is still accepted by both CLI wrappers (for --all's
    downgrade-to-warning bookkeeping and legacy-opt-out migration
    respectively) but no longer determines where the value is stored."""

    def test_dlt_write_through(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _set_dlt_telemetry

        _set_dlt_telemetry(False, tmp_path)

        config_path = tmp_path / ".dango" / "config.yml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "dlt_telemetry: false" in content

        # And the project-level legacy file must NOT be touched.
        assert not (tmp_path / ".dlt" / "config.toml").exists()

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

    def test_dlt_write_failure_raises_click_exception_not_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real OSError writing ~/.dango/config.yml (disk full,
        permissions) must surface as click.ClickException — same contract
        as _set_dbt_telemetry."""
        import click

        from dango.cli.commands.telemetry import _set_dlt_telemetry

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("builtins.open", _raise_oserror)

        with pytest.raises(click.ClickException):
            _set_dlt_telemetry(False, tmp_path)

    def test_dlt_legacy_toml_opt_out_migrated_on_first_read(self, tmp_path: Path) -> None:
        """1.0.8-OPS-2 migration decision: a pre-existing project-level
        .dlt/config.toml opt-out (dlthub_telemetry = false) is migrated
        into ~/.dango/config.yml the first time it's read through the CLI
        wrapper, rather than silently reverting to the new default. A
        second read must not require the legacy file any more."""
        import tomlkit

        from dango.cli.commands.telemetry import _get_dlt_telemetry_state

        config_path = tmp_path / ".dlt" / "config.toml"
        config_path.parent.mkdir(parents=True)
        doc = tomlkit.document()
        doc.add("runtime", tomlkit.table())
        doc["runtime"]["dlthub_telemetry"] = False
        config_path.write_text(tomlkit.dumps(doc))

        assert _get_dlt_telemetry_state(tmp_path) is False

        global_config = tmp_path / ".dango" / "config.yml"
        assert "dlt_telemetry: false" in global_config.read_text()

        # Second read must not need the legacy file at all.
        config_path.unlink()
        assert _get_dlt_telemetry_state(tmp_path) is False

    def test_dlt_legacy_toml_opt_in_needs_no_migration(self, tmp_path: Path) -> None:
        """An explicit `dlthub_telemetry = true` in the legacy file matches
        the new default already — no migration write should happen."""
        import tomlkit

        from dango.cli.commands.telemetry import _get_dlt_telemetry_state

        config_path = tmp_path / ".dlt" / "config.toml"
        config_path.parent.mkdir(parents=True)
        doc = tomlkit.document()
        doc.add("runtime", tomlkit.table())
        doc["runtime"]["dlthub_telemetry"] = True
        config_path.write_text(tomlkit.dumps(doc))

        assert _get_dlt_telemetry_state(tmp_path) is True
        assert not (tmp_path / ".dango" / "config.yml").exists()


@pytest.mark.unit
class TestDbtTelemetryConfigWriteThrough:
    """1.0.8-R: _set_dbt_telemetry()/_get_dbt_telemetry_state() now write
    through to the dbt_telemetry key in ~/.dango/config.yml via the shared
    _write_global_config_key()/_read_global_config() helpers, replacing the
    old bespoke ~/.dango/dbt_telemetry sentinel file. The error-handling
    contract is unchanged from before the consolidation: _write_global_config_key()
    itself never raises (it returns True/False), but _set_dbt_telemetry()
    checks that return value and raises click.ClickException on False —
    same raise-on-write-failure contract dlt/metabase use, so `--all` can
    report `! dbt: skipped — ...` instead of a false `✓`."""

    def test_set_dbt_telemetry_writes_config_yml_key(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_dbt_telemetry_state, _set_dbt_telemetry

        _set_dbt_telemetry(False)

        config_path = tmp_path / ".dango" / "config.yml"
        assert config_path.exists()
        assert "dbt_telemetry: false" in config_path.read_text()
        assert _get_dbt_telemetry_state() is False

    def test_get_dbt_telemetry_state_defaults_on(self, tmp_path: Path) -> None:
        from dango.cli.commands.telemetry import _get_dbt_telemetry_state

        assert _get_dbt_telemetry_state() is True

    def test_write_failure_raises_click_exception_not_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real write failure (e.g. permission denied, disk full) must
        surface as click.ClickException so `--all` can skip this provider
        and continue — not be swallowed into a false success. Routed
        through _write_global_config_key()'s False return rather than a
        raw try/except around Path.write_text() directly, since
        _set_dbt_telemetry() now delegates the write to the shared
        helper."""
        import click

        from dango.cli.commands.telemetry import _set_dbt_telemetry

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", _raise_oserror)

        with pytest.raises(click.ClickException):
            _set_dbt_telemetry(False)

    def test_write_global_config_key_returns_false_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_global_config_key() itself never raises — it signals
        failure via its return value so callers can decide whether to
        raise (dbt) or stay silent (dango's own set_telemetry_enabled())."""
        from dango.telemetry import _write_global_config_key

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", _raise_oserror)

        assert _write_global_config_key("dbt_telemetry", False) is False

    def test_write_global_config_key_returns_true_on_success(self, tmp_path: Path) -> None:
        from dango.telemetry import _write_global_config_key

        assert _write_global_config_key("dbt_telemetry", False) is True

    def test_dbt_telemetry_write_preserves_other_config_keys(self, tmp_path: Path) -> None:
        """Read-modify-write must never clobber unrelated keys already in
        config.yml — e.g. Dango's own telemetry opt-out."""
        from dango.cli.commands.telemetry import _set_dbt_telemetry
        from dango.telemetry import _write_global_config_key

        _write_global_config_key("telemetry", False)

        _set_dbt_telemetry(True)

        config_path = tmp_path / ".dango" / "config.yml"
        content = config_path.read_text()
        assert "telemetry: false" in content
        assert "dbt_telemetry: true" in content


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
    succeed for dango+dbt+dlt (all machine-level as of 1.0.8-OPS-2) and warn
    (not crash) for metabase (still project-scoped — write-through is a live
    API call using per-project credentials)."""

    def test_off_all_outside_project_warns_not_crashes(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "off", "--all"])

        assert result.exit_code == 0, result.output
        assert "dango: telemetry disabled" in result.output
        assert "dbt: telemetry disabled" in result.output
        assert "dlt: telemetry disabled" in result.output
        assert "metabase" in result.output and "skipped" in result.output

    def test_off_provider_dlt_outside_project_succeeds(self, tmp_path: Path) -> None:
        """1.0.8-OPS-2: dlt telemetry is machine-level now, so an explicit
        --provider dlt request no longer requires an active Dango project
        (unlike metabase, which still does — see the class docstring)."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "off", "--provider", "dlt"])

        assert result.exit_code == 0, result.output
        assert "dlt: telemetry disabled" in result.output

    def test_off_provider_metabase_outside_project_raises(self, tmp_path: Path) -> None:
        """A single explicit --provider request outside a project is a hard
        error, not a silent skip — this is a deliberate action, not a sweep.
        Only metabase remains project-scoped after 1.0.8-OPS-2."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["telemetry", "off", "--provider", "metabase"])

        assert result.exit_code != 0
        assert "Dango project" in result.output


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


@pytest.mark.unit
class TestLevel0DbtDltTelemetryFunctions:
    """1.0.8-U: get_dbt_telemetry_state/set_dbt_telemetry_state/
    get_dlt_telemetry_state/set_dlt_telemetry_state now live directly in
    dango.telemetry (Level 0) — cli/commands/telemetry.py's
    `_get_dbt_telemetry_state()` etc. are thin wrappers around these. Tested
    here without going through the CLI wrapper to confirm the Level-0
    functions raise plain exceptions (OSError/ValueError), not
    click.ClickException — the wrapper is responsible for that translation,
    not these functions themselves."""

    def test_get_dbt_telemetry_state_defaults_on(self) -> None:
        from dango.telemetry import get_dbt_telemetry_state

        assert get_dbt_telemetry_state() is True

    def test_set_get_dbt_telemetry_state_round_trip(self) -> None:
        from dango.telemetry import get_dbt_telemetry_state, set_dbt_telemetry_state

        set_dbt_telemetry_state(False)
        assert get_dbt_telemetry_state() is False
        set_dbt_telemetry_state(True)
        assert get_dbt_telemetry_state() is True

    def test_set_dbt_telemetry_state_raises_plain_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Level 0 has no click import — a write failure must surface as a
        plain OSError, not click.ClickException (that translation is
        cli/commands/telemetry.py's job)."""
        from dango.telemetry import set_dbt_telemetry_state

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", _raise_oserror)

        with pytest.raises(OSError):
            set_dbt_telemetry_state(False)

    def test_get_dlt_telemetry_state_defaults_on(self, tmp_path: Path) -> None:
        from dango.telemetry import get_dlt_telemetry_state

        assert get_dlt_telemetry_state(tmp_path) is True
        assert get_dlt_telemetry_state(None) is True
        assert get_dlt_telemetry_state() is True

    def test_set_get_dlt_telemetry_state_round_trip(self, tmp_path: Path) -> None:
        """1.0.8-OPS-2: dlt telemetry is machine-level (~/.dango/config.yml)
        like dbt's — set_dlt_telemetry_state() takes no project_root."""
        from dango.telemetry import get_dlt_telemetry_state, set_dlt_telemetry_state

        set_dlt_telemetry_state(False)
        assert get_dlt_telemetry_state() is False
        assert get_dlt_telemetry_state(tmp_path) is False
        set_dlt_telemetry_state(True)
        assert get_dlt_telemetry_state() is True

    def test_set_dlt_telemetry_state_raises_plain_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Level 0 has no click import — a write failure must surface as a
        plain OSError, not click.ClickException (that translation is
        cli/commands/telemetry.py's job) — same contract as
        set_dbt_telemetry_state."""
        from dango.telemetry import set_dlt_telemetry_state

        def _raise_oserror(*args: object, **kwargs: object) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", _raise_oserror)

        with pytest.raises(OSError):
            set_dlt_telemetry_state(False)

    def test_get_dlt_telemetry_state_migration_writes_global_config(self, tmp_path: Path) -> None:
        """1.0.8-OPS-2 migration decision: a pre-existing project-level
        .dlt/config.toml opt-out is migrated into ~/.dango/config.yml on
        first read, so an existing opted-out user's prior "no" answer isn't
        silently dropped by the storage move (see task file's Background /
        migration-decision section)."""
        import tomlkit

        from dango.telemetry import get_dlt_telemetry_state

        config_path = tmp_path / ".dlt" / "config.toml"
        config_path.parent.mkdir(parents=True)
        doc = tomlkit.document()
        doc.add("runtime", tomlkit.table())
        doc["runtime"]["dlthub_telemetry"] = False
        config_path.write_text(tomlkit.dumps(doc))

        result = get_dlt_telemetry_state(tmp_path)

        assert result is False
        global_config = tmp_path / ".dango" / "config.yml"
        assert global_config.exists()
        assert "dlt_telemetry: false" in global_config.read_text()

    def test_get_dlt_telemetry_state_no_migration_without_project_root(
        self, tmp_path: Path
    ) -> None:
        """Without a project_root, there's nothing to migrate from — the
        machine-level default applies, same as a brand-new install."""
        from dango.telemetry import get_dlt_telemetry_state

        assert get_dlt_telemetry_state(None) is True
        assert not (tmp_path / ".dango" / "config.yml").exists()

    def test_providers_constant(self) -> None:
        from dango.telemetry import PROVIDERS

        assert PROVIDERS == ("dango", "dbt", "dlt", "metabase")
