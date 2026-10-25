"""tests/unit/test_metabase_pin_upgrade.py

`remote upgrade` and the Metabase jar pin: an unpinned custom version fails closed before any build,
the same-version re-run resumes after the pin, a vanished helper fails the rebuild, and a downgrade
below the helper release keeps its legacy rebuild.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud.ssh import CommandResult
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/b.tar.gz"
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


def run(srv: FakeServer, version: str, **kw: Any) -> Any:
    return upgrade_dango(srv, version=version, **kw)


def _built(srv: FakeServer) -> bool:
    return any(e.endswith(":up-build") or e == "build" for e in srv.events)


def test_custom_version_upgrade_without_pin_fails_closed() -> None:
    srv = FakeServer(version="1.0.13", fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired, match="pin-metabase VERSION SHA256"):
        run(srv, "1.0.14")
    assert not _built(srv) and "legacy:pull" not in srv.events
    assert not srv.marker and srv.in_group and "FB" not in srv.events
    assert "web-start" in srv.events  # the service the upgrade stopped is started again


def test_same_version_rerun_resumes_the_migration_after_the_pin() -> None:
    srv = FakeServer(version="1.0.13", fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired):
        run(srv, "1.0.14")
    assert srv.version == "1.0.14" and not srv.marker
    del srv.fail["P2"]  # the administrator pinned the digest
    result = run(srv, "1.0.14")
    assert result.old_version == "1.0.14" == result.new_version
    assert srv.marker and not srv.in_group
    assert "helper:up-build" in srv.events and "legacy:up-build" not in srv.events
    assert srv.record == '{"dango": "1.0.14", "docker_access": "helper"}'


def test_pin_refusal_at_target_disarms_the_guard_before_any_stop() -> None:
    srv = FakeServer(version="1.0.14", fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired):
        run(srv, "1.0.14")
    assert "web-stop" not in srv.events
    assert srv.events.index("guard-arm") < srv.events.index("guard-disarm")
    assert not srv.marker and srv.in_group


class _Vanishing(FakeServer):
    """The helper marker disappears right before the Docker rebuild; the strict command exits 68."""

    def exec_command(self, command: str, timeout: int | None = None, **_k: Any) -> CommandResult:
        if f"{ch.HELPER_PATH} pull" in command or "dango-compose' pull" in command:
            self.marker = False
            if "exit 68" in command:
                self.commands.append(command)
                self.events.append("strict-refused")
                return CommandResult("", "dango-compose: verified Metabase build", 68)
        return super().exec_command(command, timeout)


def test_upgrade_requires_helper_for_metabase_rebuild() -> None:
    srv = _Vanishing(version="1.0.13")
    with pytest.raises(CloudProvisioningError):
        run(srv, "1.0.14")
    assert "legacy:pull" not in srv.events and "legacy:up-build" not in srv.events
    assert "web-start" in srv.events


def test_upgrade_with_a_matching_pin_builds_through_the_helper_only() -> None:
    srv = FakeServer(version="1.0.13")
    result = run(srv, "1.0.14")
    assert result.docker_rebuilt is True and result.warnings == []
    cmd = next(c for c in srv.commands if f"{ch.HELPER_PATH}" in c and " pull" in c)
    assert "exit 68" in cmd and "docker compose" not in cmd
    assert "helper:up-build" in srv.events and "legacy:up-build" not in srv.events


def test_pre_helper_downgrade_keeps_legacy_rebuild() -> None:
    srv = FakeServer(version="1.0.14", marker=True, in_group=False)
    run(srv, "1.0.13", force=True)
    assert srv.in_group and not srv.marker
    assert "legacy:pull" in srv.events and "legacy:up-build" in srv.events
    assert not any("exit 68" in c for c in srv.commands)
