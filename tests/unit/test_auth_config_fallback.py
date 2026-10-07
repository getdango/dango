"""tests/unit/test_auth_config_fallback.py

Tests for fail-safe auth config loading in dango/config/auth_loading.py (1.0.13-T21 / C27).
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.config.auth_loading import load_auth_config_safe
from dango.web.app import _load_auth_config

PATCH_LOGGER = "dango.config.auth_loading.logger"


def _write_project(root: Path, auth_yaml: str) -> Path:
    dango_dir = root / ".dango"
    dango_dir.mkdir()
    project_yml = dango_dir / "project.yml"
    project_yml.write_text(
        "project:\n  name: t\n  created: '2026-01-01T00:00:00Z'\n  dango_version: 1.0.0\n"
        "  organization: o\n  purpose: p\n"
        f"{auth_yaml}"
    )
    return project_yml


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


@pytest.fixture
def cloud_mode(monkeypatch):
    monkeypatch.setenv("DANGO_CLOUD_MODE", "true")


BAD_AUTH = [
    pytest.param("auth:\n  session_max_days:\n", "session_max_days", id="null"),
    pytest.param("auth:\n  session_max_days: forever\n", "session_max_days", id="string"),
    pytest.param("auth:\n  idle_timeout_minutes: soon\n", "idle_timeout_minutes", id="idle"),
]


class TestValid:
    def test_valid_config_unchanged(self, tmp_path, local_mode):
        _write_project(tmp_path, "auth:\n  session_max_days: 7\n  idle_timeout_minutes: 15\n")
        with patch(PATCH_LOGGER) as log:
            cfg = _load_auth_config(tmp_path)
        assert cfg is not None
        assert (cfg.session_max_days, cfg.idle_timeout_minutes) == (7, 15)
        log.warning.assert_not_called()

    def test_valid_config_unchanged_in_cloud_mode(self, tmp_path, cloud_mode):
        _write_project(tmp_path, "auth:\n  session_max_days: 7\n")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and cfg.session_max_days == 7

    def test_no_auth_section_gives_defaults(self, tmp_path, local_mode):
        _write_project(tmp_path, "")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and cfg.session_max_days == 365

    def test_missing_project_returns_none_without_warning(self, tmp_path, local_mode):
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_not_called()


class TestInvalidLocal:
    @pytest.mark.parametrize(("auth_yaml", "field"), BAD_AUTH)
    def test_warns_and_keeps_local_defaults(self, tmp_path, local_mode, auth_yaml, field):
        project_yml = _write_project(tmp_path, auth_yaml)
        with patch(PATCH_LOGGER) as log:
            cfg = load_auth_config_safe(tmp_path)
        assert cfg is None  # callers fall back to AuthConfig() local defaults
        log.warning.assert_called_once()
        event = log.warning.call_args
        assert event.args[0] == "auth_config_invalid"
        assert event.kwargs["file"] == str(project_yml)
        assert field in event.kwargs["field"]
        assert event.kwargs["fallback"] == "local_defaults"

    def test_invalid_yaml_warns(self, tmp_path, local_mode):
        project_yml = _write_project(tmp_path, "auth: [unclosed\n")
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["file"] == str(project_yml)

    def test_unreadable_file_warns(self, tmp_path, local_mode):
        project_yml = _write_project(tmp_path, "")
        project_yml.write_bytes(b"\xff\xfe\x00bad")
        with patch(PATCH_LOGGER) as log:
            assert load_auth_config_safe(tmp_path) is None
        log.warning.assert_called_once()


class TestInvalidCloud:
    @pytest.mark.parametrize(("auth_yaml", "field"), BAD_AUTH)
    def test_falls_back_to_cloud_values(self, tmp_path, cloud_mode, auth_yaml, field):
        _write_project(tmp_path, auth_yaml)
        with patch(PATCH_LOGGER) as log:
            cfg = _load_auth_config(tmp_path)
        assert cfg is not None
        assert cfg.session_max_days == 30
        assert cfg.idle_timeout_minutes == 60
        assert cfg.enabled is True
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["fallback"] == "cloud_defaults"

    def test_invalid_yaml_cloud_values(self, tmp_path, cloud_mode):
        _write_project(tmp_path, "auth: [unclosed\n")
        cfg = load_auth_config_safe(tmp_path)
        assert cfg is not None and (cfg.session_max_days, cfg.idle_timeout_minutes) == (30, 60)


def test_warning_does_not_echo_secret_values(tmp_path, local_mode):
    _write_project(
        tmp_path,
        "auth:\n  session_max_days: s3cret-value-xyz\n",
    )
    with patch(PATCH_LOGGER) as log:
        load_auth_config_safe(tmp_path)
    assert "s3cret-value-xyz" not in str(log.warning.call_args)
