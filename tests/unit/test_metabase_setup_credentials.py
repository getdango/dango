"""tests/unit/test_metabase_setup_credentials.py

Credential-transition tests for first-time Metabase setup.
"""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _response(status_code: int, payload: object | None = None, text: str = "") -> MagicMock:
    """Build one HTTP response mock."""
    response = MagicMock(status_code=status_code, text=text)
    response.json.return_value = payload
    return response


def _fresh_session() -> MagicMock:
    """Build the request sequence for a successful fresh Metabase setup."""
    session = MagicMock()
    session.get.side_effect = [
        _response(200, {"setup-token": "setup-token"}),
        _response(200, {"data": []}),
        _response(200, {"data": []}),
        _response(200, []),
    ]
    session.post.side_effect = [
        _response(200),  # /api/setup
        _response(200, {"id": "initial-session"}),  # login after setup
        _response(200, {"id": 7}),  # create DuckDB connection
        _response(200),  # Shared collection
        _response(200),  # Personal collection
        _response(200, {"id": "verified-session"}),  # final credential verification
    ]
    session.put.return_value = _response(200)
    return session


def _enter_setup_patches(stack: ExitStack, session: MagicMock, store: MagicMock) -> None:
    """Enter common setup patches without Docker or keychain I/O."""
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


@pytest.mark.unit
def test_fresh_setup_stages_verifies_promotes_and_writes_password_free_metadata(
    tmp_path: Path,
) -> None:
    """The candidate is protected before setup and metadata never receives it."""
    from dango.visualization.metabase import setup_metabase

    session = _fresh_session()
    store = MagicMock()
    store.load_pending.return_value = None
    events: list[str] = []
    store.save_pending.side_effect = lambda _password: events.append("stage")
    store.promote_pending.side_effect = lambda: events.append("promote")

    with ExitStack() as stack:
        _enter_setup_patches(stack, session, store)
        write_metadata = stack.enter_context(
            patch("dango.visualization.metabase.write_metabase_metadata")
        )
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")

    assert result["success"] is True
    assert result["credentials_saved"] is True
    assert events == ["stage", "promote"]
    store.save_pending.assert_called_once()
    store.promote_pending.assert_called_once_with()
    assert session.post.call_args_list[0].args[0].endswith("/api/setup")
    metadata = write_metadata.call_args.args[1]
    assert metadata["admin"] == {"email": "admin@example.com"}
    assert "password" not in metadata["admin"]


@pytest.mark.unit
def test_fresh_setup_stage_failure_makes_no_remote_setup_or_metadata_write(tmp_path: Path) -> None:
    """A secret-store failure happens before Metabase accepts the candidate."""
    from dango.visualization.metabase import setup_metabase

    session = MagicMock()
    session.get.return_value = _response(200, {"setup-token": "setup-token"})
    store = MagicMock()
    store.save_pending.side_effect = RuntimeError("secure store unavailable")

    with ExitStack() as stack:
        _enter_setup_patches(stack, session, store)
        write_metadata = stack.enter_context(
            patch("dango.visualization.metabase.write_metabase_metadata")
        )
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")

    assert result["success"] is False
    assert result["credentials_saved"] is False
    assert any("protect Metabase setup credential" in error for error in result["errors"])
    session.post.assert_not_called()
    write_metadata.assert_not_called()


@pytest.mark.unit
def test_metadata_write_failure_reports_failure_without_password_yaml(tmp_path: Path) -> None:
    """A metadata failure never claims successful credential persistence."""
    from dango.visualization.metabase import setup_metabase

    session = _fresh_session()
    store = MagicMock()
    store.load_pending.return_value = None

    with ExitStack() as stack:
        _enter_setup_patches(stack, session, store)
        stack.enter_context(
            patch(
                "dango.visualization.metabase.write_metabase_metadata",
                side_effect=OSError("disk unavailable"),
            )
        )
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")

    assert result["success"] is False
    assert result["credentials_saved"] is False
    assert any("save Metabase metadata" in error for error in result["errors"])
    assert not (tmp_path / ".dango" / "metabase.yml").exists()
    store.promote_pending.assert_called_once_with()


@pytest.mark.unit
def test_initialized_volume_recovers_verified_pending_without_setup_or_reset(
    tmp_path: Path,
) -> None:
    """A pending candidate resumes interrupted setup without creating another admin."""
    from dango.visualization.metabase import setup_metabase

    session = MagicMock()
    session.get.side_effect = [
        _response(200, {"setup-token": None}),
        _response(200, {"data": []}),
        _response(200, {"data": []}),
        _response(200, []),
    ]
    session.post.side_effect = [
        _response(200, {"id": "recovery-session"}),
        _response(200, {"id": 7}),
        _response(200),
        _response(200),
        _response(200, {"id": "verified-session"}),
    ]
    session.put.return_value = _response(200)
    store = MagicMock()
    store.load_pending.return_value = "pending-password"

    with ExitStack() as stack:
        _enter_setup_patches(stack, session, store)
        stack.enter_context(patch("dango.visualization.metabase.write_metabase_metadata"))
        docker = stack.enter_context(patch("dango.visualization.metabase.subprocess.run"))
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")

    assert result["success"] is True
    docker.assert_not_called()
    store.save_pending.assert_not_called()
    store.promote_pending.assert_called_once_with()
    assert all(not call.args[0].endswith("/api/setup") for call in session.post.call_args_list)


@pytest.mark.unit
def test_initialized_volume_retains_unverified_pending_without_reset_or_new_password(
    tmp_path: Path,
) -> None:
    """An unverified candidate remains available for a safe later recovery."""
    from dango.visualization.metabase import setup_metabase

    session = MagicMock()
    session.get.return_value = _response(200, {"setup-token": None})
    session.post.return_value = _response(401)
    store = MagicMock()
    store.load_pending.return_value = "pending-password"

    with ExitStack() as stack:
        _enter_setup_patches(stack, session, store)
        docker = stack.enter_context(patch("dango.visualization.metabase.subprocess.run"))
        result = setup_metabase(tmp_path, "Test Project", "admin@example.com")

    assert result["success"] is False
    docker.assert_not_called()
    assert any("recovery candidate was retained" in error for error in result["errors"])
    store.discard_pending.assert_not_called()
    store.promote_pending.assert_not_called()
    store.save_pending.assert_not_called()
    assert all(not call.args[0].endswith("/api/setup") for call in session.post.call_args_list)


@pytest.mark.unit
def test_startup_treats_password_free_metadata_as_configured(tmp_path: Path) -> None:
    """The startup guard does not re-run setup just because YAML has no password."""
    from dango.platform.common.startup import setup_metabase_if_needed

    metadata_dir = tmp_path / ".dango"
    metadata_dir.mkdir()
    (metadata_dir / "metabase.yml").write_text(
        "metabase_url: http://localhost:3000\nadmin:\n  email: admin@example.com\n"
    )

    with patch("dango.visualization.metabase.setup_metabase") as setup:
        result = setup_metabase_if_needed(tmp_path, "Test Project", None)

    assert result == {"already_configured": True, "success": True}
    setup.assert_not_called()
