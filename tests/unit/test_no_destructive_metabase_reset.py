"""tests/unit/test_no_destructive_metabase_reset.py

Verify Dango can no longer delete a Metabase volume or advise the user to: the reset helper
is gone, the stale-volume branch returns a non-destructive error, and no source says so.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import dango

_MESSAGE_PARTS = (
    "cannot sign in to",
    "Nothing was deleted",
    "dango metabase repair-admin",
)


def _response(status: int, payload: object = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    response.text = "user currently exists" if status == 400 else ""
    return response


def _run_setup(tmp_path: Path, session: MagicMock) -> tuple[dict[str, list[str]], MagicMock]:
    from dango.visualization.metabase import setup_metabase

    store = MagicMock()
    store.load_pending.return_value = None
    with ExitStack() as stack:
        stack.enter_context(
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-test")
        )
        stack.enter_context(
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.requests.Session", return_value=session)
        )
        stack.enter_context(
            patch("dango.visualization.metabase.MetabaseCredentialStore", return_value=store)
        )
        docker = stack.enter_context(patch("subprocess.run"))
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")
    return result, docker


@pytest.mark.unit
def test_reset_helper_no_longer_exists() -> None:
    import dango.visualization.metabase as mb

    assert not hasattr(mb, "_reset_metabase_volume")


@pytest.mark.unit
def test_initialized_metabase_with_rejected_login_returns_non_destructive_error(
    tmp_path: Path,
) -> None:
    session = MagicMock()
    session.get.return_value = _response(200, {"setup-token": None})
    session.post.return_value = _response(401)

    result, docker = _run_setup(tmp_path, session)

    assert result["success"] is False
    assert any(all(part in error for part in _MESSAGE_PARTS) for error in result["errors"])
    docker.assert_not_called()


@pytest.mark.unit
def test_failed_setup_plus_failed_login_returns_non_destructive_error(tmp_path: Path) -> None:
    session = MagicMock()
    session.get.return_value = _response(200, {"setup-token": "tok"})
    session.post.side_effect = [_response(400), _response(401)]

    result, docker = _run_setup(tmp_path, session)

    assert result["success"] is False
    assert any(all(part in error for part in _MESSAGE_PARTS) for error in result["errors"])
    assert all("docker volume rm" not in error for error in result["errors"])
    docker.assert_not_called()


@pytest.mark.unit
def test_no_source_file_instructs_or_performs_a_volume_removal() -> None:
    root = Path(dango.__file__).parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "docker_audit.py"
        and ("docker volume rm" in path.read_text() or "_reset_metabase_volume" in path.read_text())
    ]
    assert offenders == []
