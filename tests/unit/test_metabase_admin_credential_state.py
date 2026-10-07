"""tests/unit/test_metabase_admin_credential_state.py

Verify metabase_admin_credential_state classifies every credential state without repairing.
It makes at most one login per call, never raises, and never exposes the password.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.platform.common.metabase_credential_state import metabase_admin_credential_state

_LOADER = "dango.security.metabase_config.load_metabase_admin_credentials"
_SECRET = "s3cret-pass-do-not-leak"


def _write_project(root: Path, *, email: str | None = "admin@example.com") -> None:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"metabase_url": "http://localhost:3000"}
    if email is not None:
        metadata["admin"] = {"email": email}
    (root / ".dango" / "metabase.yml").write_text(yaml.safe_dump(metadata), encoding="utf-8")
    (root / ".dango" / "project.yml").write_text(
        yaml.safe_dump({"project": {"id": "a" * 32}}), encoding="utf-8"
    )


def _response(status: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    return response


def _state(
    root: Path,
    *,
    health: int | Exception = 200,
    login: int | Exception = 200,
    credentials: object = ("admin@example.com", _SECRET),
) -> tuple[str, MagicMock, MagicMock]:
    def _get(*_a: object, **_k: object) -> MagicMock:
        if isinstance(health, Exception):
            raise health
        return _response(health)

    def _post(*_a: object, **_k: object) -> MagicMock:
        if isinstance(login, Exception):
            raise login
        return _response(login)

    loader = (
        {"side_effect": credentials}
        if isinstance(credentials, Exception)
        else {"return_value": credentials}
    )
    with (
        patch(_LOADER, **loader),
        patch("requests.get", side_effect=_get) as get,
        patch("requests.post", side_effect=_post) as post,
    ):
        state = metabase_admin_credential_state(root)
    return state, get, post


@pytest.mark.unit
def test_state_not_configured(tmp_path: Path) -> None:
    state, get, post = _state(tmp_path)
    assert state == "not_configured"
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
def test_state_not_configured_without_admin_email(tmp_path: Path) -> None:
    _write_project(tmp_path, email=None)
    state, _get, post = _state(tmp_path)
    assert state == "not_configured"
    post.assert_not_called()


@pytest.mark.unit
def test_state_ok(tmp_path: Path) -> None:
    _write_project(tmp_path)
    state, get, post = _state(tmp_path)
    assert state == "ok"
    assert get.call_count == 1
    assert post.call_count == 1


@pytest.mark.unit
def test_state_probe_false_makes_no_http_call(tmp_path: Path) -> None:
    _write_project(tmp_path)
    with (
        patch(_LOADER, return_value=("admin@example.com", _SECRET)),
        patch("requests.get") as get,
        patch("requests.post") as post,
    ):
        state = metabase_admin_credential_state(tmp_path, probe=False)
    assert state == "unverified"
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
def test_state_missing(tmp_path: Path) -> None:
    _write_project(tmp_path)
    state, get, post = _state(tmp_path, credentials=None)
    assert state == "missing"
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
def test_state_unreadable(tmp_path: Path) -> None:
    _write_project(tmp_path)
    state, get, post = _state(tmp_path, credentials=RuntimeError("malformed store"))
    assert state == "unreadable"
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
def test_state_rejected(tmp_path: Path, status: int) -> None:
    _write_project(tmp_path)
    state, _get, post = _state(tmp_path, login=status)
    assert state == "rejected"
    assert post.call_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("health", "login", "expected_posts"),
    [
        (503, 200, 0),
        (ConnectionError("down"), 200, 0),
        (200, 429, 1),
        (200, 500, 1),
        (200, ConnectionError("reset"), 1),
    ],
)
def test_state_unreachable_is_silent(
    tmp_path: Path,
    health: int | Exception,
    login: int | Exception,
    expected_posts: int,
) -> None:
    _write_project(tmp_path)
    state, get, post = _state(tmp_path, health=health, login=login)
    assert state == "unreachable"
    assert get.call_count == 1
    assert post.call_count == expected_posts


@pytest.mark.unit
def test_state_never_exposes_password(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_project(tmp_path)
    for login in (200, 401, 500):
        state, _get, _post = _state(tmp_path, login=login)
        assert _SECRET not in state
    captured = capsys.readouterr()
    assert _SECRET not in captured.out + captured.err


@pytest.mark.unit
def test_state_never_raises_on_unexpected_error(tmp_path: Path) -> None:
    _write_project(tmp_path)
    (tmp_path / ".dango" / "metabase.yml").write_text("- not\n- a mapping\n", encoding="utf-8")
    state, _get, post = _state(tmp_path)
    assert state == "unreachable"
    post.assert_not_called()
