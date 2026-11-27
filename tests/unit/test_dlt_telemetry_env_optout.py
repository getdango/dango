"""tests/unit/test_dlt_telemetry_env_optout.py

dlt telemetry honours the user's env opt-outs (DO_NOT_TRACK, DANGO_TELEMETRY,
an explicit RUNTIME__DLTHUB_TELEMETRY=false) as well as ~/.dango/config.yml.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import dango.telemetry as dango_telemetry
from dango.ingestion.dlt_runner import _apply_dlt_telemetry_env
from dango.telemetry import get_dlt_telemetry_state

_DLT_ENV = "RUNTIME__DLTHUB_TELEMETRY"
_MARKER = dango_telemetry._DLT_ENV_WRITTEN_MARKER
_ENV_VARS = ("DO_NOT_TRACK", "DANGO_TELEMETRY", _DLT_ENV, _MARKER)
_TRUTHY = ("1", "t", "true", "y", "yes", "TRUE", " Yes ")
_FALSY = ("0", "f", "false", "n", "no", "FALSE", " No ")


@pytest.fixture(autouse=True)
def _empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty throwaway ~/.dango (no config key) and none of the opt-out env vars set.

    os.environ is snapshotted and restored whole: _apply_dlt_telemetry_env()
    writes the process env directly, which monkeypatch alone would not undo.
    """
    saved = dict(os.environ)
    fake_home = tmp_path / "_home"
    fake_home.mkdir()
    config_dir = fake_home / ".dango"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(dango_telemetry, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(dango_telemetry, "_IDENTITY_FILE", config_dir / "telemetry.json")
    monkeypatch.setattr(dango_telemetry, "_GLOBAL_CONFIG_FILE", config_dir / "config.yml")
    for var in (*_ENV_VARS, *dango_telemetry._CI_ENV_VARS):
        os.environ.pop(var, None)
    yield fake_home
    os.environ.clear()
    os.environ.update(saved)


def _write_config(fake_home: Path, text: str) -> Path:
    path = fake_home / ".dango" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.mark.unit
class TestDltTelemetryStateEnvOptOut:
    """get_dlt_telemetry_state() with an empty ~/.dango/config.yml."""

    def test_default_without_env_or_config_is_on(self, tmp_path: Path) -> None:
        assert get_dlt_telemetry_state(tmp_path) is True

    @pytest.mark.parametrize("value", _TRUTHY)
    def test_do_not_track_truthy_turns_dlt_off(self, tmp_path: Path, value: str) -> None:
        os.environ["DO_NOT_TRACK"] = value
        assert get_dlt_telemetry_state(tmp_path) is False

    @pytest.mark.parametrize("value", (*_FALSY, ""))
    def test_do_not_track_falsy_or_empty_leaves_default(self, tmp_path: Path, value: str) -> None:
        os.environ["DO_NOT_TRACK"] = value
        assert get_dlt_telemetry_state(tmp_path) is True

    @pytest.mark.parametrize("value", _FALSY)
    def test_dango_telemetry_falsy_turns_dlt_off(self, tmp_path: Path, value: str) -> None:
        os.environ["DANGO_TELEMETRY"] = value
        assert get_dlt_telemetry_state(tmp_path) is False

    @pytest.mark.parametrize("value", _TRUTHY)
    def test_dango_telemetry_truthy_leaves_default(self, tmp_path: Path, value: str) -> None:
        os.environ["DANGO_TELEMETRY"] = value
        assert get_dlt_telemetry_state(tmp_path) is True

    @pytest.mark.parametrize("value", _FALSY)
    def test_explicit_dlt_env_false_turns_dlt_off(self, tmp_path: Path, value: str) -> None:
        os.environ[_DLT_ENV] = value
        assert get_dlt_telemetry_state(tmp_path) is False

    @pytest.mark.parametrize("value", _TRUTHY)
    def test_explicit_dlt_env_true_with_no_config_stays_on(
        self, tmp_path: Path, value: str
    ) -> None:
        os.environ[_DLT_ENV] = value
        assert get_dlt_telemetry_state(tmp_path) is True


@pytest.mark.unit
class TestConfigOptOutAlwaysWins:
    """An env value never turns on what ~/.dango/config.yml turned off."""

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            (_DLT_ENV, "true"),
            (_DLT_ENV, "1"),
            ("DANGO_TELEMETRY", "true"),
            ("DANGO_TELEMETRY", "1"),
            ("DO_NOT_TRACK", "0"),
            ("DO_NOT_TRACK", "false"),
        ],
    )
    def test_config_false_stays_false_whatever_the_env_says(
        self, _empty_home: Path, tmp_path: Path, var: str, value: str
    ) -> None:
        _write_config(_empty_home, "dlt_telemetry: false\n")
        os.environ[var] = value
        assert get_dlt_telemetry_state(tmp_path) is False

    def test_config_true_is_still_overridden_by_an_env_opt_out(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        _write_config(_empty_home, "dlt_telemetry: true\n")
        os.environ["DO_NOT_TRACK"] = "1"
        assert get_dlt_telemetry_state(tmp_path) is False

    def test_env_check_never_writes_the_machine_config(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        os.environ["DO_NOT_TRACK"] = "1"
        os.environ["DANGO_TELEMETRY"] = "false"
        assert get_dlt_telemetry_state(tmp_path) is False
        assert not (_empty_home / ".dango" / "config.yml").exists()

    def test_config_with_other_keys_is_not_modified_by_the_env_check(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        path = _write_config(_empty_home, "telemetry: true\ndbt_telemetry: true\n")
        before = path.read_bytes()
        os.environ["DO_NOT_TRACK"] = "1"
        assert get_dlt_telemetry_state(tmp_path) is False
        assert path.read_bytes() == before

    def test_legacy_toml_opt_out_still_migrates_without_env(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        legacy = tmp_path / ".dlt" / "config.toml"
        legacy.parent.mkdir()
        legacy.write_text("[runtime]\ndlthub_telemetry = false\n", encoding="utf-8")
        assert get_dlt_telemetry_state(tmp_path) is False
        config_text = (_empty_home / ".dango" / "config.yml").read_text(encoding="utf-8")
        assert "dlt_telemetry: false" in config_text


@pytest.mark.unit
class TestApplyDltTelemetryEnv:
    """_apply_dlt_telemetry_env() leaves the process env var dlt reads."""

    def test_default_sets_true(self, tmp_path: Path) -> None:
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "true"

    @pytest.mark.parametrize("value", ("1", "true", "yes"))
    def test_do_not_track_sets_false(self, tmp_path: Path, value: str) -> None:
        os.environ["DO_NOT_TRACK"] = value
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

    @pytest.mark.parametrize("value", ("0", "false", "no"))
    def test_dango_telemetry_falsy_sets_false(self, tmp_path: Path, value: str) -> None:
        os.environ["DANGO_TELEMETRY"] = value
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

    @pytest.mark.parametrize("value", _FALSY)
    def test_explicit_dlt_env_false_is_never_overwritten_with_true(
        self, tmp_path: Path, value: str
    ) -> None:
        os.environ[_DLT_ENV] = value
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV].strip().lower() in dango_telemetry._FALSY_ENV_VALUES

    def test_config_false_beats_an_explicit_env_true(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        _write_config(_empty_home, "dlt_telemetry: false\n")
        os.environ[_DLT_ENV] = "true"
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

    def test_explicit_env_true_with_no_opt_out_stays_true(self, tmp_path: Path) -> None:
        os.environ[_DLT_ENV] = "true"
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "true"


@pytest.mark.unit
class TestDangoOwnEnvValueIsNotAUserOptOut:
    """A value Dango wrote into the env var earlier must not stick as an opt-out."""

    def test_turning_telemetry_back_on_takes_effect_in_the_same_process(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        _write_config(_empty_home, "dlt_telemetry: false\n")
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

        _write_config(_empty_home, "dlt_telemetry: true\n")
        assert get_dlt_telemetry_state(tmp_path) is True
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "true"

    def test_a_child_process_inheriting_dangos_false_follows_the_config(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        """The marker is an env var, so a child sees which value was Dango's."""
        _write_config(_empty_home, "dlt_telemetry: false\n")
        _apply_dlt_telemetry_env(tmp_path)
        inherited = dict(os.environ)

        os.environ.clear()
        os.environ.update(inherited)
        _write_config(_empty_home, "dlt_telemetry: true\n")
        assert get_dlt_telemetry_state(tmp_path) is True

    def test_users_explicit_false_survives_repeated_applies_and_a_config_turned_on(
        self, _empty_home: Path, tmp_path: Path
    ) -> None:
        os.environ[_DLT_ENV] = "false"
        _apply_dlt_telemetry_env(tmp_path)
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

        _write_config(_empty_home, "dlt_telemetry: true\n")
        assert get_dlt_telemetry_state(tmp_path) is False
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"

    def test_an_env_opt_out_removes_a_stale_marker(self, tmp_path: Path) -> None:
        """A user opt-out must not leave Dango's marker behind (it would hide a later explicit false)."""
        os.environ[_MARKER] = "false"
        os.environ[_DLT_ENV] = "false"
        os.environ["DO_NOT_TRACK"] = "1"
        _apply_dlt_telemetry_env(tmp_path)
        assert os.environ[_DLT_ENV] == "false"
        assert _MARKER not in os.environ


@pytest.fixture
def _fresh_dlt_runtime_cleaned(
    fresh_dlt_runtime: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """A fresh dlt runtime in a throwaway working dir, torn down again afterwards.

    Resets dlt's per-process telemetry flag
    (``dlt.common.runtime.telemetry._TELEMETRY_STARTED``, via
    ``stop_telemetry()``) and the cached ``PluggableRunContext`` (via the
    shared ``fresh_dlt_runtime`` fixture). DNS is blocked for the whole test
    so no telemetry event can leave the process.
    """
    from dlt.common.configuration.container import Container
    from dlt.common.configuration.specs.pluggable_run_context import PluggableRunContext
    from dlt.common.runtime import telemetry

    def _no_network(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_NONAME, "network disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))
    yield
    telemetry.stop_telemetry()
    container = Container()
    if PluggableRunContext in container:
        del container[PluggableRunContext]


@pytest.mark.unit
@pytest.mark.usefixtures("_fresh_dlt_runtime_cleaned")
class TestRealDltPipelineResolvesTelemetry:
    """A real dlt.pipeline() created after _apply_dlt_telemetry_env() (no events are sent)."""

    @staticmethod
    def _create_pipeline_and_resolve(tmp_path: Path) -> tuple[bool, Any]:
        import dlt
        from dlt.common.configuration import resolve_configuration
        from dlt.common.configuration.specs import RuntimeConfiguration
        from dlt.common.runtime import anon_tracker, telemetry

        _apply_dlt_telemetry_env(tmp_path)
        dlt.pipeline(
            "wp69_env_optout",
            pipelines_dir=str(tmp_path / "pipelines"),
            destination=dlt.destinations.duckdb(str(tmp_path / "wp69.duckdb")),
        )
        assert telemetry.is_telemetry_started() is True
        resolved = resolve_configuration(RuntimeConfiguration()).dlthub_telemetry
        return resolved, anon_tracker._ANON_TRACKER_ENDPOINT

    def test_do_not_track_disables_dlt_telemetry(self, tmp_path: Path) -> None:
        os.environ["DO_NOT_TRACK"] = "1"
        resolved, endpoint = self._create_pipeline_and_resolve(tmp_path)
        assert resolved is False
        assert endpoint is None

    def test_explicit_dlt_env_false_disables_dlt_telemetry(self, tmp_path: Path) -> None:
        os.environ[_DLT_ENV] = "false"
        resolved, endpoint = self._create_pipeline_and_resolve(tmp_path)
        assert resolved is False
        assert endpoint is None

    def test_positive_control_default_keeps_dlt_telemetry_on(self, tmp_path: Path) -> None:
        resolved, endpoint = self._create_pipeline_and_resolve(tmp_path)
        assert resolved is True
        assert endpoint is not None
