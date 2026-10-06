"""tests/unit/test_docker_leak_support.py

Unit tests for the real-Docker test teardown/leak helpers (1.0.13-T7), with subprocess mocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.integration import docker_leak_support as mod

_RUN = "tests.integration.docker_leak_support.subprocess.run"


def _done(rc: int = 0, out: str = "", err: str = "") -> MagicMock:
    return MagicMock(returncode=rc, stdout=out, stderr=err)


def test_leftover_volume_is_reported_by_name() -> None:
    with patch(_RUN, side_effect=[_done(out="dango-x_metabase-data\n"), _done(rc=1)]):
        with pytest.raises(AssertionError, match="dango-x_metabase-data"):
            mod.assert_no_docker_leftovers("dango-x")


def test_leftover_image_is_reported_by_name() -> None:
    with patch(_RUN, side_effect=[_done(), _done(rc=0)]):
        with pytest.raises(AssertionError, match="image:dango-x-metabase"):
            mod.assert_no_docker_leftovers("dango-x")


def test_clean_state_passes() -> None:
    with patch(_RUN, side_effect=[_done(), _done(rc=1)]):
        mod.assert_no_docker_leftovers("dango-x")


def test_missing_docker_raises_clearly() -> None:
    with patch(_RUN, side_effect=FileNotFoundError("docker")):
        with pytest.raises(FileNotFoundError):
            mod.assert_no_docker_leftovers("dango-x")


def test_down_uses_real_env_and_rmi_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setenv("HOME", "/real/home")
    with patch(_RUN, return_value=_done()) as run:
        mod.compose_down_and_prune(tmp_path, "dango-x")
    args, kwargs = run.call_args
    assert args[0][-4:] == ["down", "-v", "--rmi", "local"]
    assert kwargs["env"]["COMPOSE_PROJECT_NAME"] == "dango-x"
    assert kwargs["env"]["HOME"] == "/real/home"


def test_down_failure_is_not_silent(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    with patch(_RUN, return_value=_done(rc=1, err="unknown command")):
        with pytest.raises(AssertionError, match="unknown command"):
            mod.compose_down_and_prune(tmp_path, "dango-x")


def test_down_skipped_without_compose_file(tmp_path: Path) -> None:
    with patch(_RUN) as run:
        mod.compose_down_and_prune(tmp_path, "dango-x")
    run.assert_not_called()


_ = subprocess  # keep the patched module attribute importable
