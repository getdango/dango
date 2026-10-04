"""tests/unit/test_upgrade_credential_preparation.py

Verify offline Metabase credential preparation during ``dango upgrade``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "project.yml").write_text(
        "project:\n  name: test\n  version: '1.0'\n", encoding="utf-8"
    )
    return root


def _mock_pip(mock_subprocess: MagicMock) -> None:
    mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")


@pytest.mark.unit
class TestUpgradeCredentialPreparation:
    """Offline legacy-Metabase preparation behavior for ``dango upgrade``."""

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending")
    @patch(
        "dango.platform.common.metabase_credential_migration.prepare_metabase_credential_migration"
    )
    @patch("dango.__version__", "0.1.0")
    def test_preparation_follows_migrations_before_success_output(
        self,
        mock_preparation: MagicMock,
        mock_migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        root = _project(tmp_path)
        mock_root.return_value = root
        events: list[str] = []

        def record_pip_call(command: list[str], **_kwargs: object) -> MagicMock:
            events.append("pip_install" if "install" in command else "pip_check")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.run.side_effect = record_pip_call
        mock_migrations.side_effect = lambda _root: events.append("migrations") or {}
        mock_preparation.side_effect = lambda _root: (
            events.append("preparation")
            or {
                "version": 1,
                "status": "prepared",
            }
        )
        result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        assert result.exit_code == 0
        assert events == ["pip_check", "pip_install", "migrations", "preparation"]
        assert (
            result.output.index("Running database migrations")
            < result.output.index("Legacy Metabase credentials will be secured")
            < result.output.index("Upgrade complete")
        )

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending", return_value={})
    @patch(
        "dango.platform.common.metabase_credential_migration.prepare_metabase_credential_migration",
        return_value={"version": 1, "status": "failed_non_destructive"},
    )
    @patch("dango.__version__", "0.1.0")
    def test_preparation_failure_preserves_successful_upgrade(
        self,
        _preparation: MagicMock,
        _migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_root.return_value = _project(tmp_path)
        _mock_pip(mock_subprocess)

        result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        assert result.exit_code == 0
        assert "credential preparation did not complete" in result.output
        assert "dango start" in result.output
        assert "Upgrade complete" in result.output

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending", return_value={})
    @patch("dango.__version__", "0.1.0")
    def test_legacy_no_id_upgrade_records_only_offline_preparation_state(
        self,
        _migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _project(tmp_path)
        metadata = root / ".dango" / "metabase.yml"
        metadata.write_text(
            "admin:\n  email: admin@example.com\n  password: legacy-secret\n", encoding="utf-8"
        )
        mock_root.return_value = root
        _mock_pip(mock_subprocess)

        import requests

        import dango.platform.common.metabase_credential_migration as migration
        import dango.platform.docker as docker

        monkeypatch.setattr(
            migration,
            "MetabaseCredentialStore",
            lambda _root: pytest.fail("offline preparation must not open credential storage"),
        )
        monkeypatch.setattr(
            docker,
            "DockerManager",
            lambda *_args, **_kwargs: pytest.fail("offline preparation must not use Docker"),
        )
        monkeypatch.setattr(
            requests,
            "post",
            lambda *_args, **_kwargs: pytest.fail(
                "offline preparation must not make HTTP requests"
            ),
        )

        result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        state = root / ".dango" / "state" / "metabase_credential_migration.json"
        assert result.exit_code == 0
        assert json.loads(state.read_text()) == {"status": "prepared", "version": 1}
        assert "legacy-secret" not in state.read_text()
        assert "legacy-secret" in metadata.read_text()

    @pytest.mark.parametrize(
        ("ignore_result", "expected_message"),
        [
            (True, "Added sensitive local-artifact ignore rules to .gitignore"),
            (False, ""),
        ],
    )
    def test_upgrade_applies_gitignore_hardening_without_affecting_preparation(
        self,
        ignore_result: bool,
        expected_message: str,
        tmp_path: Path,
    ) -> None:
        root = _project(tmp_path)
        with (
            patch("dango.cli.utils.find_project_root", return_value=root),
            patch("dango.cli.commands.upgrade.subprocess") as mock_subprocess,
            patch("dango.migrations.apply_all_pending", return_value={}),
            patch(
                "dango.platform.common.metabase_credential_migration.prepare_metabase_credential_migration",
                return_value={"version": 1, "status": "not_needed"},
            ) as mock_preparation,
            patch(
                "dango.config.credentials.ensure_sensitive_artifact_gitignores",
                return_value=ignore_result,
            ) as mock_ignores,
            patch("dango.__version__", "0.1.0"),
        ):
            _mock_pip(mock_subprocess)
            result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        assert result.exit_code == 0
        mock_preparation.assert_called_once_with(root)
        mock_ignores.assert_called_once_with(root)
        if expected_message:
            assert expected_message in result.output
        else:
            assert "Added sensitive local-artifact ignore rules" not in result.output

    def test_gitignore_failure_preserves_successful_upgrade_and_preparation(
        self, tmp_path: Path
    ) -> None:
        root = _project(tmp_path)
        with (
            patch("dango.cli.utils.find_project_root", return_value=root),
            patch("dango.cli.commands.upgrade.subprocess") as mock_subprocess,
            patch("dango.migrations.apply_all_pending", return_value={}),
            patch(
                "dango.platform.common.metabase_credential_migration.prepare_metabase_credential_migration",
                return_value={"version": 1, "status": "not_needed"},
            ) as mock_preparation,
            patch(
                "dango.config.credentials.ensure_sensitive_artifact_gitignores",
                side_effect=OSError("permission denied"),
            ),
            patch("dango.__version__", "0.1.0"),
        ):
            _mock_pip(mock_subprocess)
            result = CliRunner().invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        assert result.exit_code == 0
        mock_preparation.assert_called_once_with(root)
        assert "Could not update .gitignore" in result.output
        assert "dango start" in result.output
        assert "permission denied" not in result.output
        assert "Upgrade complete" in result.output
