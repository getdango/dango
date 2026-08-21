"""tests/unit/test_google_sheets_source.py

Unit tests for google_sheets source empty range handling.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestGoogleSheetsEmptyRange:
    """Test that empty Google Sheets ranges raise RuntimeError instead of silently skipping."""

    @pytest.fixture(autouse=True)
    def setup_credentials(self, monkeypatch):
        """Set up mock Google credentials via environment."""
        # Create a minimal valid service account JSON
        creds_json = {
            "type": "service_account",
            "project_id": "test-project",
            "private_key_id": "key-id",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygvFdB0dShW1x4Y1OfAhXmYy8PqjZb7x\nhQ+YrqFfBhBZhLFmOOZp3qKZdCCT4yy7Ync6uyNvLfW8EFWvWQaQG7ZH5L85bvhH\neSNc7RzMFHKGvCqSZtbfvLsVHZHxg3fOGKf6JR4gFPLo3X9YXs1MZlUCAwEAAQKC\nAQB/Ej4gSmRpSH0wkW+7/DQOVsKFAJ7HpNB/xLxVpJ6qbH3k3q2OqcTcQhCQhvZN\njFXLqPl3qLYl6h7T2s7E7VQxP1+S/JfXUW7CJPE/XfZdD1Y5E1P8/v3fP2YMfH+T\nxM3qM1q9I5ZqM8P4c2RLxKQxq0Z7P2SWXkQF48CqUQKBgQDsEZ5VLbZ4qVVaLqKl\nq3IFjkKvGPx9QVFZ7nU1B8QdkJqjdXKpVx3E7Aw9LLPw6M8YjIVzHfV3Fz2ZWCWa\nGtUxDaD8UL5QQ8PdZjJZVzZ/XBjVLyNLXl8Z5qNvU9LnvLxJwcKPDQpEU8K0S4kQ\nQW8X5Q7L8xXOjBfQjQKBgQDgJnZPP5p8QU5xz8Y5TlG3pxVWJVFVDzN7z7x5Y7Zh\nxW3LH2T0DxV3F5xL5VPUVyNg7L8aFVVqZJjL2p3o6F0q/lXxe8G6Y3QXvZAOqD9l\n0gqX7KpvLjJE+t4fTaYXV5TvFXSE1YY0P8H5r7qYJ0N9W2x8c7mZFzlKwQKBgDHo\nPBxZbsn3RJLx5XvP2X2GF7H/+4b8YN3vQQvVvFh3Zn7nJ7Y0q/dAW8PvP8kXCnJq\nX3qJqCjmEVVQu2F6/qVl6fPY4Z9eNKzMQvE8wR3cKJQ/lLrxLqH8LvLTlH6nJFxP\nLqNOV0c5xJqI3FxWqpHQKlKKvVFQJOYPkZQKBgQCXtx6E1p8uIqJvT7yoMrZJKXL5\nIJCf3BYz7yXd4CVCFMy8Y5E5VZVzEvRh7qY/wFg2Fl3W2n2V4I7Z1qvKI6pQcMBu\nFGLbEWQsVy9mFh6yZxsH8Yw4P1t1k0sY7fJpqB5gQKBgQCsAaHxn5K3dBj5Y1Yx\nJP8T5QQfv+3RY0F8qKBrQBzhANWQUtLl3pj8NkLZFWVqL8yXmCUmKVFfNzOxNgzj\nQ==\n-----END RSA PRIVATE KEY-----",
            "client_email": "test@test-project.iam.gserviceaccount.com",
            "client_id": "1234567890",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        monkeypatch.setenv(
            "SOURCES__GOOGLE_SHEETS__CREDENTIALS",
            json.dumps(creds_json),
        )

    def _make_source(self, all_range_data):
        """Helper to create a source with mocked API calls."""
        from dango.ingestion.dlt_sources.google_sheets import google_spreadsheet

        sheet_names = ["Sheet1"]
        spreadsheet_title = "Test Spreadsheet"

        with (
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_auth",
                return_value=MagicMock(),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_known_range_names",
                return_value=(sheet_names, [], spreadsheet_title),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_data_for_ranges",
                return_value=all_range_data,
            ),
        ):
            source = google_spreadsheet(
                spreadsheet_url_or_id="test-id",
                range_names=["MySheet"],
            )
            return list(source)

    def test_empty_range_raises(self):
        """Test that a range with no data (values=None) raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), None),  # values=None
        ]
        with pytest.raises(RuntimeError, match="returned no data"):
            self._make_source(all_range_data)

    def test_empty_values_list_raises(self):
        """Test that a range with empty values list (values=[]) raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), []),  # values=[]
        ]
        with pytest.raises(RuntimeError, match="returned no data"):
            self._make_source(all_range_data)

    def test_header_only_range_raises(self):
        """Test that a range with only a header row raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), [["id", "amount"]]),  # header only
        ]
        with pytest.raises(RuntimeError, match="only a header row"):
            self._make_source(all_range_data)

    def test_error_message_includes_range_name(self):
        """Test that the error message includes the range name."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), []),
        ]
        with pytest.raises(RuntimeError) as exc_info:
            self._make_source(all_range_data)
        assert "MySheet" in str(exc_info.value)
