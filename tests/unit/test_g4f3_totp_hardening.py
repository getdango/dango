"""tests/unit/test_g4f3_totp_hardening.py

TOTP verification keeps a persistent account-level failure counter (not reset by a password
login), logs failures, refuses a replayed time-step, and /2fa/disable needs a TOTP code.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.auth_2fa import _2fa_attempt_counts
from tests.unit.test_web_auth_2fa import (
    _TEST_PASSWORD,
    _authed,
    _client,
    _enable_2fa,
    _hdrs,
    _make_app,
    _make_db,
    _make_user,
)

_WRONG = "000000"


@pytest.fixture(autouse=True)
def _clean_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _2fa_attempt_counts.clear()
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))


def _away_from_step_boundary() -> None:
    """Codes are checked against the wall clock: do not straddle a 30 s time-step change."""
    if time.time() % 30 > 26:
        time.sleep(5)


def _project(tmp_path: Path) -> tuple[TestClient, Path, str, str]:
    """Client, db, user id and TOTP secret for a user with 2FA on."""
    db_path = _make_db(tmp_path)
    user = _make_user(db_path)
    secret = _enable_2fa(db_path, user.id)
    return _client(_make_app(db_path)), db_path, user.id, secret


def _password_login(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/login", json={"email": "test@example.com", "password": _TEST_PASSWORD}
    )
    assert resp.status_code == 200 and resp.json()["requires_2fa"] is True
    cookie = resp.cookies.get(COOKIE_NAME)
    assert cookie
    return cookie


def _verify(client: TestClient, cookie: str, code: str, **extra: Any) -> Any:
    return client.post(
        "/api/auth/2fa/verify",
        json={"code": code, "is_recovery": False, **extra},
        cookies={COOKIE_NAME: cookie},
        headers=_hdrs(),
    )


@pytest.mark.unit
class TestPersistentTotpLockout:
    def test_failures_survive_a_fresh_password_login(self, tmp_path: Path) -> None:
        client, _db, _uid, secret = _project(tmp_path)
        cookie = _password_login(client)
        for _ in range(5):
            assert _verify(client, cookie, _WRONG).status_code in (400, 401, 423)

        # "login -> guess" again: the account counter was not reset by the password success
        cookie2 = _password_login(client)
        locked = _verify(client, cookie2, pyotp.TOTP(secret).now())
        assert locked.status_code == 423
        # no full session was issued: the cookie is not replaced
        assert locked.cookies.get(COOKIE_NAME) in (None, cookie2)

    def test_counter_is_persistent_not_in_process(self, tmp_path: Path) -> None:
        client, _db, _uid, secret = _project(tmp_path)
        for _ in range(2):
            cookie = _password_login(client)
            for _ in range(3):
                _verify(client, cookie, _WRONG)
            _2fa_attempt_counts.clear()  # as if another worker (or a restart) served the next call
        cookie = _password_login(client)
        assert _verify(client, cookie, pyotp.TOTP(secret).now()).status_code == 423

    def test_a_legitimate_user_stays_unlocked_after_success(self, tmp_path: Path) -> None:
        _away_from_step_boundary()
        client, _db, _uid, secret = _project(tmp_path)
        totp = pyotp.TOTP(secret)
        # three rounds of "three typos, then the right code"; each right code is of a later
        # time-step than the one before, so replay protection does not interfere
        for offset in (-30, 0, 30):
            cookie = _password_login(client)
            for _ in range(3):
                assert _verify(client, cookie, _WRONG).status_code == 400
            ok = _verify(client, cookie, totp.at(time.time() + offset))
            assert ok.status_code == 200, ok.text
            _2fa_attempt_counts.clear()

    def test_failed_totp_is_audited_without_the_code(self, tmp_path: Path) -> None:
        client, _db, _uid, _secret = _project(tmp_path)
        cookie = _password_login(client)
        _verify(client, cookie, "123456")
        audit = tmp_path / ".dango" / "logs" / "audit.jsonl"
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        failures = [e for e in events if e["event"] == "login_failure"]
        assert any(e.get("details", {}).get("reason") == "invalid_totp" for e in failures)
        assert "123456" not in audit.read_text()


@pytest.mark.unit
class TestTotpReplay:
    def test_a_code_cannot_be_used_twice(self, tmp_path: Path) -> None:
        _away_from_step_boundary()
        client, _db, _uid, secret = _project(tmp_path)
        code = pyotp.TOTP(secret).now()
        assert _verify(client, _password_login(client), code).status_code == 200
        replay = _verify(client, _password_login(client), code)
        assert replay.status_code in (400, 401)

    def test_the_next_time_step_is_accepted(self, tmp_path: Path) -> None:
        _away_from_step_boundary()
        client, _db, _uid, secret = _project(tmp_path)
        totp = pyotp.TOTP(secret)
        assert _verify(client, _password_login(client), totp.now()).status_code == 200
        later = totp.at(time.time() + 30)
        assert _verify(client, _password_login(client), later).status_code == 200


@pytest.mark.unit
class TestDisableNeedsTotp:
    def test_password_alone_does_not_disable(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = client.post(
            "/api/auth/2fa/disable", json={"password": _TEST_PASSWORD}, headers=_hdrs()
        )
        assert resp.status_code == 400
        current = db.get_user_by_id(db_path, user.id)
        assert current is not None and current.totp_enabled is True

    def test_wrong_code_does_not_disable(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        resp = client.post(
            "/api/auth/2fa/disable",
            json={"password": _TEST_PASSWORD, "code": _WRONG},
            headers=_hdrs(),
        )
        assert resp.status_code == 400
        current = db.get_user_by_id(db_path, user.id)
        assert current is not None and current.totp_enabled is True

    def test_password_and_code_disable(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        secret = _enable_2fa(db_path, user.id)
        resp = client.post(
            "/api/auth/2fa/disable",
            json={"password": _TEST_PASSWORD, "code": pyotp.TOTP(secret).now()},
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        current = db.get_user_by_id(db_path, user.id)
        assert current is not None and current.totp_enabled is False
