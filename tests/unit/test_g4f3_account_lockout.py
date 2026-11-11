"""tests/unit/test_g4f3_account_lockout.py

Failed logins are also counted per account, so guessing spread across many source addresses
locks the account; password-checking endpoints share that counter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.lockout import (
    check_account_locked,
    record_failed_login,
    reset_failed_logins,
    unlock_account,
)
from tests.unit.test_auth_lockout import _get_user_row, _make_db, _make_user
from tests.unit.test_web_auth_2fa import (
    _TEST_PASSWORD,
    _authed,
    _enable_2fa,
    _hdrs,
    _make_app,
)
from tests.unit.test_web_auth_2fa import (
    _make_db as _make_web_db,
)
from tests.unit.test_web_auth_2fa import (
    _make_user as _make_web_user,
)

_EMAIL = "test@example.com"


def _spray(
    db_path: Path, email: str, ips: int, per_ip: int, *, first_ip: int = 0, max_attempts: int = 5
) -> None:
    for n in range(first_ip, first_ip + ips):
        for _ in range(per_ip):
            record_failed_login(
                db_path, email, client_ip=f"10.0.{n // 250}.{n % 250}", max_attempts=max_attempts
            )


@pytest.mark.unit
class TestAccountWideCounter:
    def test_fifty_ips_four_attempts_each_locks_the_account(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        db.create_user(db_path, _make_user())
        _spray(db_path, _EMAIL, ips=50, per_ip=4)

        locked, remaining = check_account_locked(db_path, _EMAIL, client_ip="203.0.113.77")
        assert locked is True and remaining > 0
        # independent of the address asking, and of the address-less lookup
        assert check_account_locked(db_path, _EMAIL)[0] is True

    def test_unknown_email_locks_the_same_way(self, tmp_path: Path) -> None:
        """Locking must not reveal whether the account exists."""
        db_path = _make_db(tmp_path)
        _spray(db_path, "nobody@example.com", ips=50, per_ip=4)
        assert check_account_locked(db_path, "nobody@example.com", client_ip="203.0.113.77")[0]

    def test_users_counter_adds_up_across_addresses(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        db.create_user(db_path, _make_user())
        _spray(db_path, _EMAIL, ips=3, per_ip=4)
        assert _get_user_row(db_path, _EMAIL)["failed_login_attempts"] == 12
        assert check_account_locked(db_path, _EMAIL, client_ip="203.0.113.77")[0] is False

    def test_success_clears_the_account_counter(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        db.create_user(db_path, _make_user())
        _spray(db_path, _EMAIL, ips=3, per_ip=4)
        reset_failed_logins(db_path, _EMAIL, client_ip="10.0.0.0")
        assert _get_user_row(db_path, _EMAIL)["failed_login_attempts"] == 0
        # a fresh run of failures from new addresses starts counting from zero again
        _spray(db_path, _EMAIL, ips=3, per_ip=4, first_ip=100)
        assert check_account_locked(db_path, _EMAIL, client_ip="203.0.113.77")[0] is False

    def test_admin_unlock_clears_the_account_lock(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        db.create_user(db_path, _make_user())
        _spray(db_path, _EMAIL, ips=50, per_ip=4)
        assert unlock_account(db_path, _EMAIL) is True
        assert check_account_locked(db_path, _EMAIL, client_ip="203.0.113.77")[0] is False


@pytest.mark.unit
def test_login_route_lockout_message_is_identical_for_known_and_unknown_emails(
    tmp_path: Path,
) -> None:
    """One failure per address; once the account total is reached, a new address is refused."""
    db_path = _make_web_db(tmp_path)
    _make_web_user(db_path)
    (tmp_path / ".dango" / "project.yml").write_text("auth:\n  lockout:\n    max_attempts: 2\n")
    app = _make_app(db_path)

    def attempt(email: str, password: str, ip: str) -> Any:
        client = TestClient(app, client=(ip, 5000), raise_server_exceptions=False)
        return client.post("/api/auth/login", json={"email": email, "password": password})

    bodies = {}
    for email in (_EMAIL, "nobody@example.com"):
        # max_attempts 2 per address, so the account locks at 8 failures in total
        for n in range(7):
            assert attempt(email, "wrong-password-1", f"198.51.100.{n + 1}").status_code == 400
        assert attempt(email, "wrong-password-1", "198.51.100.8").status_code == 423
        final = attempt(email, _TEST_PASSWORD, "198.51.100.200")
        assert final.status_code == 423, email
        bodies[email] = {k: v for k, v in final.json().items() if k != "remaining_seconds"}
    assert bodies[_EMAIL] == bodies["nobody@example.com"]


_NEW_PASSWORD = "a-Brand-New-pass-77!"


_ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("change-password", "/api/auth/change-password", {"new_password": _NEW_PASSWORD}),
    ("2fa-setup", "/api/auth/2fa/setup", {}),
    ("2fa-disable", "/api/auth/2fa/disable", {"code": "000000"}),
    ("2fa-regenerate", "/api/auth/2fa/regenerate-recovery", {"code": "000000"}),
]


@pytest.mark.unit
class TestPasswordCheckingEndpointsAreLimited:
    @staticmethod
    def _body(name: str, extra: dict[str, Any], password: str) -> dict[str, Any]:
        key = "current_password" if name == "change-password" else "password"
        return {key: password, **extra}

    @pytest.mark.parametrize(("name", "path", "extra"), _ENDPOINTS, ids=[e[0] for e in _ENDPOINTS])
    def test_repeated_wrong_passwords_lock_the_account(
        self, tmp_path: Path, name: str, path: str, extra: dict[str, Any]
    ) -> None:
        client, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        statuses = [
            client.post(path, json=self._body(name, extra, "wrong-password"), headers=_hdrs())
            for _ in range(5)
        ]
        assert [r.status_code for r in statuses[:4]] == [400] * 4
        assert statuses[4].status_code == 423
        # the real password is refused while locked, and so is a normal login
        ok = client.post(path, json=self._body(name, extra, _TEST_PASSWORD), headers=_hdrs())
        assert ok.status_code == 423
        login = client.post(
            "/api/auth/login", json={"email": user.email, "password": _TEST_PASSWORD}
        )
        assert login.status_code == 423
        assert check_account_locked(db_path, user.email, client_ip="testclient")[0] is True

    def test_a_correct_password_clears_earlier_mistakes(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        for _ in range(3):
            assert (
                client.post(
                    "/api/auth/2fa/setup", json={"password": "wrong-password"}, headers=_hdrs()
                ).status_code
                == 400
            )
        ok = client.post("/api/auth/2fa/setup", json={"password": _TEST_PASSWORD}, headers=_hdrs())
        assert ok.status_code == 200
        assert _get_user_row(db_path, user.email)["failed_login_attempts"] == 0
        for _ in range(3):
            client.post("/api/auth/2fa/setup", json={"password": "wrong-password"}, headers=_hdrs())
        assert check_account_locked(db_path, user.email, client_ip="testclient")[0] is False
