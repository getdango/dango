"""tests/unit/test_deploy_journal.py

Unit tests for dango/platform/cloud/deploy_journal.py — deployment journal.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dango.platform.cloud.deploy_journal import (
    DeploymentRecord,
    get_latest_deployment,
    read_local_journal,
    read_remote_journal,
    write_local_journal,
    write_remote_journal,
)
from dango.platform.cloud.ssh import CommandResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides: object) -> DeploymentRecord:
    """Create a DeploymentRecord with sensible defaults."""
    defaults = {
        "timestamp": "2026-03-27T12:00:00+00:00",
        "deployer": "test@host",
        "success": True,
        "git_commit": "a" * 40,
        "git_branch": "main",
        "git_clean": True,
        "dango_version": "1.0.0",
        "files_synced": ["dbt/models/"],
        "models_changed": ["stg_orders"],
        "models_added": [],
        "models_removed": [],
        "duration_seconds": 42.5,
        "dry_run": False,
        "error": None,
    }
    defaults.update(overrides)
    return DeploymentRecord(**defaults)  # type: ignore[arg-type]


def _make_ssh_mock() -> MagicMock:
    """Return a mock SSHManager."""
    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    return ssh


# ---------------------------------------------------------------------------
# DeploymentRecord serialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeploymentRecord:
    """Test record serialization."""

    def test_to_dict(self) -> None:
        record = _make_record()
        d = asdict(record)
        assert d["timestamp"] == "2026-03-27T12:00:00+00:00"
        assert d["deployer"] == "test@host"
        assert d["success"] is True
        assert d["git_commit"] == "a" * 40

    def test_to_json(self) -> None:
        record = _make_record()
        json_str = json.dumps(asdict(record))
        parsed = json.loads(json_str)
        assert parsed["git_branch"] == "main"
        assert parsed["duration_seconds"] == 42.5

    def test_optional_fields_none(self) -> None:
        record = _make_record(git_commit=None, git_branch=None, error=None)
        d = asdict(record)
        assert d["git_commit"] is None
        assert d["error"] is None


# ---------------------------------------------------------------------------
# Local journal
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLocalJournal:
    """Test local journal write/read."""

    def test_write_creates_file(self, tmp_path: Path) -> None:
        record = _make_record()
        write_local_journal(tmp_path, record)

        journal = tmp_path / ".dango" / "state" / "deployments.jsonl"
        assert journal.exists()
        lines = journal.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["deployer"] == "test@host"

    def test_write_appends(self, tmp_path: Path) -> None:
        write_local_journal(tmp_path, _make_record(deployer="first"))
        write_local_journal(tmp_path, _make_record(deployer="second"))

        journal = tmp_path / ".dango" / "state" / "deployments.jsonl"
        lines = journal.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["deployer"] == "first"
        assert json.loads(lines[1])["deployer"] == "second"

    def test_write_never_raises_on_permission_error(self, tmp_path: Path) -> None:
        # Make parent read-only
        state_dir = tmp_path / ".dango" / "state"
        state_dir.mkdir(parents=True)
        journal = state_dir / "deployments.jsonl"
        journal.touch()
        journal.chmod(0o000)

        # Should not raise
        write_local_journal(tmp_path, _make_record())

        journal.chmod(0o644)  # Cleanup

    def test_read_empty(self, tmp_path: Path) -> None:
        result = read_local_journal(tmp_path)
        assert result == []

    def test_read_returns_newest_first(self, tmp_path: Path) -> None:
        write_local_journal(tmp_path, _make_record(deployer="first"))
        write_local_journal(tmp_path, _make_record(deployer="second"))

        result = read_local_journal(tmp_path)
        assert len(result) == 2
        assert result[0]["deployer"] == "second"
        assert result[1]["deployer"] == "first"

    def test_read_respects_limit(self, tmp_path: Path) -> None:
        for i in range(5):
            write_local_journal(tmp_path, _make_record(deployer=f"deploy-{i}"))

        result = read_local_journal(tmp_path, limit=2)
        assert len(result) == 2
        assert result[0]["deployer"] == "deploy-4"

    def test_read_handles_corrupt_lines(self, tmp_path: Path) -> None:
        journal = tmp_path / ".dango" / "state" / "deployments.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(asdict(_make_record(deployer="good"))) + "\n"
            "not valid json\n" + json.dumps(asdict(_make_record(deployer="also-good"))) + "\n"
        )

        result = read_local_journal(tmp_path)
        assert len(result) == 2
        assert result[0]["deployer"] == "also-good"
        assert result[1]["deployer"] == "good"


# ---------------------------------------------------------------------------
# Remote journal
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteJournal:
    """Test remote journal write/read."""

    def test_write_calls_ssh(self) -> None:
        ssh = _make_ssh_mock()
        record = _make_record()
        write_remote_journal(ssh, record)

        ssh.exec_command.assert_called_once()
        cmd = ssh.exec_command.call_args[0][0]
        assert "deployments.jsonl" in cmd
        assert "mkdir -p" in cmd

    def test_write_never_raises(self) -> None:
        ssh = _make_ssh_mock()
        ssh.exec_command.side_effect = Exception("SSH failure")

        # Should not raise
        write_remote_journal(ssh, _make_record())

    def test_read_returns_newest_first(self) -> None:
        ssh = _make_ssh_mock()
        line1 = json.dumps(asdict(_make_record(deployer="first")))
        line2 = json.dumps(asdict(_make_record(deployer="second")))
        ssh.exec_command.return_value = CommandResult(
            stdout=f"{line1}\n{line2}\n", stderr="", exit_code=0
        )

        result = read_remote_journal(ssh)
        assert len(result) == 2
        assert result[0]["deployer"] == "second"

    def test_read_empty_journal(self) -> None:
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=1)

        result = read_remote_journal(ssh)
        assert result == []

    def test_read_handles_corrupt_lines(self) -> None:
        ssh = _make_ssh_mock()
        good = json.dumps(asdict(_make_record(deployer="good")))
        ssh.exec_command.return_value = CommandResult(
            stdout=f"not json\n{good}\n", stderr="", exit_code=0
        )

        result = read_remote_journal(ssh)
        assert len(result) == 1
        assert result[0]["deployer"] == "good"


# ---------------------------------------------------------------------------
# get_latest_deployment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetLatestDeployment:
    """Test get_latest_deployment."""

    def test_returns_last_entry(self) -> None:
        ssh = _make_ssh_mock()
        entry = asdict(_make_record(deployer="latest"))
        ssh.exec_command.return_value = CommandResult(
            stdout=json.dumps(entry) + "\n", stderr="", exit_code=0
        )

        result = get_latest_deployment(ssh)
        assert result is not None
        assert result["deployer"] == "latest"

    def test_returns_none_on_empty(self) -> None:
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=1)

        result = get_latest_deployment(ssh)
        assert result is None

    def test_returns_none_on_corrupt(self) -> None:
        ssh = _make_ssh_mock()
        ssh.exec_command.return_value = CommandResult(stdout="not json", stderr="", exit_code=0)

        result = get_latest_deployment(ssh)
        assert result is None
