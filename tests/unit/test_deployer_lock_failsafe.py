"""tests/unit/test_deployer_lock_failsafe.py

The deploy lock fails safe: an unreadable lock file or expiry is treated as held, not free.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.deployer import (
    DEPLOY_LOCK_PATH,
    LOCK_TIMEOUT_MINUTES,
    DeployLock,
    _acquire_lock,
    _check_existing_lock,
    _is_lock_expired,
)
from tests.factories.interruption_fakes import FakeServer

pytestmark = pytest.mark.unit

_MAX_AGE = LOCK_TIMEOUT_MINUTES * 60


def _lock_json(**overrides: str) -> str:
    now = datetime.now(tz=timezone.utc)
    data = {
        "deployer": "other@host",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=20)).isoformat(),
    }
    data.update(overrides)
    return json.dumps(data)


def _acquire(server: FakeServer, *, force: bool = False) -> DeployLock:
    return _acquire_lock(cast(Any, server), force=force)


@pytest.mark.parametrize("content", ["{not json", "[]", '"text"', "42", "null"])
def test_corrupt_lock_file_counts_as_held(content: str) -> None:
    server = FakeServer(lock=content)
    with patch("dango.platform.cloud.deployer.logger") as log:
        with pytest.raises(CloudProvisioningError) as excinfo:
            _acquire(server)
    message = str(excinfo.value)
    assert "unreadable" in message
    assert DEPLOY_LOCK_PATH in message
    assert "--force" in message
    assert server.lock == content, "a corrupt lock must not be removed or replaced"
    assert log.warning.called


def test_corrupt_lock_file_can_be_overridden_with_force() -> None:
    server = FakeServer(lock="{not json")
    lock = _acquire(server, force=True)
    assert server.lock is not None and json.loads(server.lock)["deployer"] == lock.deployer


def test_check_existing_lock_reports_a_corrupt_file_as_a_lock() -> None:
    lock = _check_existing_lock(cast(Any, FakeServer(lock="{not json")))
    assert lock is not None
    assert lock.readable is False


@pytest.mark.parametrize("expires", ["not-a-date", "", "2026-13-45T99:00:00"])
def test_unparseable_expiry_is_held_while_the_file_is_recent(expires: str) -> None:
    server = FakeServer(lock=_lock_json(expires_at=expires), lock_age_seconds=60)
    with patch("dango.platform.cloud.deployer.logger") as log:
        with pytest.raises(CloudProvisioningError, match="Deploy lock held"):
            _acquire(server)
    assert server.lock is not None and json.loads(server.lock)["deployer"] == "other@host"
    assert log.warning.called


def test_unparseable_expiry_is_held_when_the_file_age_is_unknown() -> None:
    server = FakeServer(lock=_lock_json(expires_at="not-a-date"), lock_age_seconds=None)
    with pytest.raises(CloudProvisioningError, match="Deploy lock held"):
        _acquire(server)


def test_unparseable_expiry_is_taken_over_once_the_file_is_older_than_the_lock_lifetime() -> None:
    server = FakeServer(lock=_lock_json(expires_at="not-a-date"), lock_age_seconds=_MAX_AGE + 60)
    lock = _acquire(server)
    assert server.lock is not None and json.loads(server.lock)["deployer"] == lock.deployer


def test_is_lock_expired_needs_the_file_age_when_the_expiry_is_unparseable() -> None:
    lock = DeployLock(deployer="x", started_at="", expires_at="not-a-date")
    assert _is_lock_expired(lock) is False
    assert _is_lock_expired(lock, file_age_seconds=10) is False
    assert _is_lock_expired(lock, file_age_seconds=_MAX_AGE + 1) is True


def test_a_parseable_expiry_still_decides() -> None:
    past = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(tz=timezone.utc) + timedelta(minutes=1)).isoformat()
    assert _is_lock_expired(DeployLock("x", "", past)) is True
    assert _is_lock_expired(DeployLock("x", "", future), file_age_seconds=10_000) is False
