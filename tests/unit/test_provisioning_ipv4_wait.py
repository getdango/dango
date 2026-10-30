"""tests/unit/test_provisioning_ipv4_wait.py

wait_for_droplet_ready keeps polling until the droplet is active AND has a public IPv4,
within the existing wait budget (provisioning.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.provisioning import provision_droplet, wait_for_droplet_ready

_NO_IP: dict[str, Any] = {"id": 1, "status": "active", "networks": {"v4": [], "v6": []}}
_PRIVATE_ONLY: dict[str, Any] = {
    "id": 1,
    "status": "active",
    "networks": {"v4": [{"type": "private", "ip_address": "10.0.0.2"}]},
}
_WITH_IP: dict[str, Any] = {
    "id": 1,
    "status": "active",
    "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.7"}]},
}


class _Clock:
    """Fake monotonic clock advanced by the patched sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Any:
    fake = _Clock()
    with (
        patch("dango.platform.cloud.provisioning.time.monotonic", fake.monotonic),
        patch("dango.platform.cloud.provisioning.time.sleep", fake.sleep),
    ):
        yield fake


@pytest.mark.unit
class TestWaitForPublicIPv4:
    def test_active_without_ip_then_ip_appears(self, clock):
        client = MagicMock()
        client.get_droplet.side_effect = [_NO_IP, _PRIVATE_ONLY, _WITH_IP]

        result = wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=120.0)

        assert result == _WITH_IP
        assert client.get_droplet.call_count == 3
        assert clock.sleeps == [5.0, 5.0]

    def test_new_then_active_without_ip_then_ip(self, clock):
        client = MagicMock()
        client.get_droplet.side_effect = [{"id": 1, "status": "new"}, _NO_IP, _WITH_IP]

        result = wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=120.0)

        assert result == _WITH_IP
        assert client.get_droplet.call_count == 3

    def test_ip_never_appears_reports_active_without_public_ipv4(self, clock):
        client = MagicMock()
        client.get_droplet.return_value = _NO_IP

        with pytest.raises(CloudError) as exc_info:
            wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=30.0)

        message = str(exc_info.value)
        assert "active" in message
        assert "public IPv4" in message
        assert client.get_droplet.call_count > 1
        assert clock.now >= 30.0

    def test_never_active_still_reports_not_active(self, clock):
        client = MagicMock()
        client.get_droplet.return_value = {"id": 1, "status": "new"}

        with pytest.raises(CloudError) as exc_info:
            wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=30.0)

        assert exc_info.value.error_code == "DANGO-D011"
        assert "did not become active" in str(exc_info.value)

    def test_errored_while_waiting_for_ip_raises(self, clock):
        client = MagicMock()
        client.get_droplet.side_effect = [_NO_IP, {"id": 1, "status": "errored"}]

        with pytest.raises(CloudError) as exc_info:
            wait_for_droplet_ready(client, 1, poll_interval=5.0, timeout=120.0)

        assert exc_info.value.error_code == "DANGO-D010"


@pytest.mark.unit
class TestProvisionDropletWaitsForIPv4:
    def test_late_ip_does_not_fail_or_delete_the_droplet(self, clock):
        client = MagicMock()
        client.create_droplet.return_value = {"id": 1, "status": "new"}
        client.get_droplet.side_effect = [_NO_IP, _NO_IP, _WITH_IP]

        with patch("dango.platform.cloud.provisioning.wait_for_ssh") as mock_ssh:
            result = provision_droplet(client, "n", "sgp1", "s-2vcpu-4gb", ssh_key_ids=[])

        assert result == _WITH_IP
        mock_ssh.assert_called_once_with("203.0.113.7")
        client.delete_droplet.assert_not_called()

    def test_ip_never_appears_deletes_droplet_and_reports_it(self, clock):
        client = MagicMock()
        client.create_droplet.return_value = {"id": 1, "status": "new"}
        client.get_droplet.return_value = _NO_IP

        with pytest.raises(CloudError, match="public IPv4"):
            provision_droplet(client, "n", "sgp1", "s-2vcpu-4gb", ssh_key_ids=[])

        client.delete_droplet.assert_called_once_with(1)
