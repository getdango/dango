"""tests/unit/test_service_account_wizard.py

Tests for service account authentication wizard in oauth.py
"""

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import click
import pytest


class TestServiceAccountWizard:
    """Tests for _try_service_account_auth helper function"""

    def test_valid_service_account_json_saves_credential(self, tmp_path: Path) -> None:
        """Service account JSON is validated and saved with correct fields"""
        from dango.cli.commands.oauth import _try_service_account_auth

        # Create a valid service account JSON file
        service_account_key = {
            "type": "service_account",
            "project_id": "test-project-123",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...",
            "client_email": "test-service@test-project-123.iam.gserviceaccount.com",
            "client_id": "123456789",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        key_file = tmp_path / "service_account.json"
        key_file.write_text(json.dumps(service_account_key))

        # Mock the inquirer module
        mock_inquirer = Mock()
        mock_select = Mock()
        mock_select.execute.return_value = "sa"
        mock_text = Mock()
        mock_text.execute.return_value = str(key_file)
        mock_inquirer.select = Mock(return_value=mock_select)
        mock_inquirer.text = Mock(return_value=mock_text)

        mock_inquirerpy = Mock()
        mock_inquirerpy.inquirer = mock_inquirer

        with patch.dict(sys.modules, {"InquirerPy": mock_inquirerpy}):
            with patch("dango.oauth.storage.OAuthStorage.save", return_value=True):
                result = _try_service_account_auth(
                    source_type="google_sheets", project_root=tmp_path
                )

        assert result is True

    def test_invalid_json_raises_abort(self, tmp_path: Path) -> None:
        """Invalid JSON file content raises click.Abort"""
        from dango.cli.commands.oauth import _try_service_account_auth

        # Create an invalid JSON file
        key_file = tmp_path / "bad_key.json"
        key_file.write_text("{invalid json content")

        mock_inquirer = Mock()
        mock_select = Mock()
        mock_select.execute.return_value = "sa"
        mock_text = Mock()
        mock_text.execute.return_value = str(key_file)
        mock_inquirer.select = Mock(return_value=mock_select)
        mock_inquirer.text = Mock(return_value=mock_text)

        mock_inquirerpy = Mock()
        mock_inquirerpy.inquirer = mock_inquirer

        with patch.dict(sys.modules, {"InquirerPy": mock_inquirerpy}):
            with pytest.raises(click.Abort):
                _try_service_account_auth(source_type="google_sheets", project_root=tmp_path)

    def test_missing_required_fields_raises_abort(self, tmp_path: Path) -> None:
        """Missing required fields in JSON raises click.Abort"""
        from dango.cli.commands.oauth import _try_service_account_auth

        # Create JSON missing 'private_key'
        incomplete_key = {
            "type": "service_account",
            "project_id": "test-project",
            "client_email": "test@test.iam.gserviceaccount.com",
        }
        key_file = tmp_path / "incomplete_key.json"
        key_file.write_text(json.dumps(incomplete_key))

        mock_inquirer = Mock()
        mock_select = Mock()
        mock_select.execute.return_value = "sa"
        mock_text = Mock()
        mock_text.execute.return_value = str(key_file)
        mock_inquirer.select = Mock(return_value=mock_select)
        mock_inquirer.text = Mock(return_value=mock_text)

        mock_inquirerpy = Mock()
        mock_inquirerpy.inquirer = mock_inquirer

        with patch.dict(sys.modules, {"InquirerPy": mock_inquirerpy}):
            with pytest.raises(click.Abort):
                _try_service_account_auth(source_type="google_sheets", project_root=tmp_path)

    def test_wrong_type_raises_abort(self, tmp_path: Path) -> None:
        """Wrong service account type raises click.Abort"""
        from dango.cli.commands.oauth import _try_service_account_auth

        # Create JSON with wrong type
        wrong_type_key = {
            "type": "authorized_user",
            "project_id": "test-project",
            "private_key": "key",
            "client_email": "test@test.iam.gserviceaccount.com",
        }
        key_file = tmp_path / "wrong_type.json"
        key_file.write_text(json.dumps(wrong_type_key))

        mock_inquirer = Mock()
        mock_select = Mock()
        mock_select.execute.return_value = "sa"
        mock_text = Mock()
        mock_text.execute.return_value = str(key_file)
        mock_inquirer.select = Mock(return_value=mock_select)
        mock_inquirer.text = Mock(return_value=mock_text)

        mock_inquirerpy = Mock()
        mock_inquirerpy.inquirer = mock_inquirer

        with patch.dict(sys.modules, {"InquirerPy": mock_inquirerpy}):
            with pytest.raises(click.Abort):
                _try_service_account_auth(source_type="google_sheets", project_root=tmp_path)

    def test_oauth_choice_returns_false(self, tmp_path: Path) -> None:
        """Selecting 'OAuth' returns False without calling save"""
        from dango.cli.commands.oauth import _try_service_account_auth

        mock_inquirer = Mock()
        mock_select = Mock()
        mock_select.execute.return_value = "oauth"
        mock_inquirer.select = Mock(return_value=mock_select)

        mock_inquirerpy = Mock()
        mock_inquirerpy.inquirer = mock_inquirer

        with patch.dict(sys.modules, {"InquirerPy": mock_inquirerpy}):
            result = _try_service_account_auth(source_type="google_sheets", project_root=tmp_path)

        assert result is False
