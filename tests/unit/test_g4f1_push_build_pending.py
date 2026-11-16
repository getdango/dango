"""tests/unit/test_g4f1_push_build_pending.py

I8-10: a push that copied files but did not finish building leaves the build outstanding.

After the copy, a retry of the identical push finds equal files on both sides and plans no
dbt work.  The retry must still run ``dbt deps`` and a full ``dbt run`` until a build succeeds.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.deployer import push_deploy
from dango.platform.cloud.file_sync import SyncResult
from dango.platform.cloud.ssh import CommandResult

_SERVICE_WORDS = (
    "systemctl",
    "chown",
    "runuser",
    "sudo",
    "venv",
    "docker",
    "curl",
    "rsync",
    "journalctl",
    "python",
)


@pytest.fixture(autouse=True)
def _trusted_server_venv() -> Any:
    with patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]):
        yield


class FakeServer:
    """SSH fake: dbt commands are scripted; plain file commands run in a sandbox ``/srv/dango``."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "srv" / "dango"
        self.root.mkdir(parents=True)
        self.dbt: list[str] = []
        self.failing: set[str] = set()
        self.key_path = Path("/tmp/test_key")

    def exec_command(self, command: str, timeout: int | None = None, **_: object) -> CommandResult:
        match = re.search(r"/dbt (deps|compile|run)\b", command)
        if match:
            self.dbt.append(match.group(1))
            ok = match.group(1) not in self.failing
            return CommandResult(stdout="", stderr="" if ok else "boom", exit_code=0 if ok else 1)
        if "/srv/dango" in command and not any(w in command for w in _SERVICE_WORDS):
            proc = subprocess.run(
                ["sh", "-c", command.replace("/srv/dango", str(self.root))],
                capture_output=True,
                text=True,
                check=False,
            )
            return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
        return CommandResult(stdout="", stderr="", exit_code=0)

    def write_remote_file(self, *_: object, **__: object) -> None:
        return None


def _push(server: FakeServer, tmp_path: Path, sync: SyncResult) -> Any:
    backup = MagicMock(warnings=[])
    with (
        patch("dango.platform.cloud.file_sync.sync_project_files", return_value=sync),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
    ):
        return push_deploy(server, tmp_path, "10.0.0.1")


def _changed() -> SyncResult:
    return SyncResult(synced_files=["dbt/models/"], changed_models=["stg_orders"])


def _identical() -> SyncResult:
    return SyncResult(synced_files=["dbt/models/"])


@pytest.mark.unit
class TestRetryAfterIncompleteBuild:
    def test_retry_after_failed_run_rebuilds(self, tmp_path: Path) -> None:
        server = FakeServer(tmp_path)
        server.failing = {"run"}
        with pytest.raises(CloudProvisioningError):
            _push(server, tmp_path, _changed())

        server.failing = set()
        server.dbt.clear()
        result = _push(server, tmp_path, _identical())  # same files: nothing looks changed

        assert "deps" in server.dbt and "run" in server.dbt
        assert result.models_rebuilt

    def test_retry_after_failed_compile_rebuilds(self, tmp_path: Path) -> None:
        server = FakeServer(tmp_path)
        server.failing = {"compile"}
        with pytest.raises(CloudProvisioningError):
            _push(server, tmp_path, SyncResult(synced_files=["dbt/"], has_macro_changes=True))

        server.failing = set()
        server.dbt.clear()
        _push(server, tmp_path, _identical())
        assert "run" in server.dbt

    def test_nothing_is_rebuilt_after_a_successful_build(self, tmp_path: Path) -> None:
        server = FakeServer(tmp_path)
        _push(server, tmp_path, _changed())
        server.dbt.clear()

        result = _push(server, tmp_path, _identical())

        assert "run" not in server.dbt and "deps" not in server.dbt
        assert result.models_rebuilt == []

    def test_a_forced_rebuild_clears_the_pending_state_on_success(self, tmp_path: Path) -> None:
        server = FakeServer(tmp_path)
        server.failing = {"run"}
        with pytest.raises(CloudProvisioningError):
            _push(server, tmp_path, _changed())
        server.failing = set()
        _push(server, tmp_path, _identical())
        server.dbt.clear()

        _push(server, tmp_path, _identical())

        assert "run" not in server.dbt

    def test_failed_deps_keep_the_rebuild_pending(self, tmp_path: Path) -> None:
        server = FakeServer(tmp_path)
        server.failing = {"run"}
        with pytest.raises(CloudProvisioningError):
            _push(server, tmp_path, _changed())
        server.failing = {"deps"}
        _push(server, tmp_path, _identical())  # deps fails: only a warning, build is not proven
        server.failing = set()
        server.dbt.clear()

        _push(server, tmp_path, _identical())

        assert "deps" in server.dbt and "run" in server.dbt
