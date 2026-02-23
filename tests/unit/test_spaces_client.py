"""tests/unit/test_spaces_client.py

Unit tests for SpacesClient (dango/platform/cloud/spaces.py).

boto3 is injected via sys.modules patching so these tests run without
installing the [cloud] extra.  botocore.exceptions.ClientError is used
for the real exception class (botocore is a boto3 dependency, but it is also
used directly to construct ClientError in tests).
"""

from __future__ import annotations

import io
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudAuthError, CloudError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_boto3_mock() -> tuple[MagicMock, MagicMock]:
    """Return (boto3_module_mock, s3_client_mock) with a usable ClientError."""

    # We need a real-ish ClientError so _get_boto_error_code works.
    class _FakeClientError(Exception):
        def __init__(self, error_response: dict[str, Any], operation_name: str) -> None:
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(str(error_response))

    s3_client = MagicMock()
    boto3_mock = MagicMock()
    boto3_mock.client.return_value = s3_client
    # Attach the fake ClientError so spaces.py can catch it by attribute
    boto3_mock.exceptions = MagicMock()
    boto3_mock.exceptions.ClientError = _FakeClientError

    # Store ClientError on the s3 client too (boto3 attaches it to the client)
    s3_client.exceptions = MagicMock()
    s3_client.exceptions.ClientError = _FakeClientError

    return boto3_mock, s3_client, _FakeClientError


def _inject_boto3(boto3_mock: MagicMock):
    """Context manager: inject boto3_mock into sys.modules."""
    return patch.dict(sys.modules, {"boto3": boto3_mock})


def _make_spaces_client(**kwargs: Any):
    """Import SpacesClient fresh (after sys.modules is patched)."""
    from dango.platform.cloud.spaces import SpacesClient

    return SpacesClient(**kwargs)


# ---------------------------------------------------------------------------
# 1. Initialisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSpacesClientInit:
    def test_creds_from_arguments(self):
        """Credentials supplied directly are stored without env lookup."""
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(
            bucket="my-bucket",
            region="nyc3",
            access_key="key123",
            secret_key="secret456",
        )
        assert client._access_key == "key123"
        assert client._secret_key == "secret456"
        assert client.bucket == "my-bucket"
        assert client.region == "nyc3"

    def test_creds_from_env_vars(self, monkeypatch):
        """Credentials are read from the default env vars."""
        monkeypatch.setenv("SPACES_ACCESS_KEY", "env-key")
        monkeypatch.setenv("SPACES_SECRET_KEY", "env-secret")

        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3")
        assert client._access_key == "env-key"
        assert client._secret_key == "env-secret"

    def test_custom_env_var_names(self, monkeypatch):
        """Custom env var names are respected."""
        monkeypatch.setenv("MY_KEY", "custom-key")
        monkeypatch.setenv("MY_SECRET", "custom-secret")

        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(
            bucket="b",
            region="nyc3",
            access_key_env="MY_KEY",
            secret_key_env="MY_SECRET",
        )
        assert client._access_key == "custom-key"
        assert client._secret_key == "custom-secret"

    def test_missing_access_key_raises(self, monkeypatch):
        """CloudAuthError raised when access key is missing."""
        monkeypatch.delenv("SPACES_ACCESS_KEY", raising=False)
        monkeypatch.setenv("SPACES_SECRET_KEY", "secret")

        from dango.platform.cloud.spaces import SpacesClient

        with pytest.raises(CloudAuthError, match="access key"):
            SpacesClient(bucket="b", region="nyc3")

    def test_missing_secret_key_raises(self, monkeypatch):
        """CloudAuthError raised when secret key is missing."""
        monkeypatch.setenv("SPACES_ACCESS_KEY", "key")
        monkeypatch.delenv("SPACES_SECRET_KEY", raising=False)

        from dango.platform.cloud.spaces import SpacesClient

        with pytest.raises(CloudAuthError, match="secret key"):
            SpacesClient(bucket="b", region="nyc3")


# ---------------------------------------------------------------------------
# 2. boto3 import behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBoto3Missing:
    def test_import_error_raises_cloud_error(self, monkeypatch):
        """CloudError with install hint when boto3 is not installed."""
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with patch.dict(sys.modules, {"boto3": None}):  # type: ignore[dict-item]
            # Force _s3_client to None so _get_client() runs
            client._s3_client = None
            with pytest.raises(CloudError, match="pip install getdango\\[cloud\\]"):
                client._get_client()

    def test_get_client_cached_after_first_call(self):
        """_get_client() returns the same client object on subsequent calls."""
        boto3_mock, s3_client, _ = _make_boto3_mock()

        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            c1 = client._get_client()
            c2 = client._get_client()

        assert c1 is c2
        assert boto3_mock.client.call_count == 1


# ---------------------------------------------------------------------------
# 3. Upload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpload:
    def _client_with_mock_s3(self) -> tuple[Any, MagicMock]:
        boto3_mock, s3_client, _ = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            client._get_client()  # prime cache
        return client, s3_client

    def test_upload_bytes(self):
        """upload() calls put_object with bytes data."""
        client, s3 = self._client_with_mock_s3()
        client.upload("backup/db.duckdb", b"binary data")
        s3.put_object.assert_called_once_with(
            Bucket="b",
            Key="backup/db.duckdb",
            Body=b"binary data",
            ContentType="application/octet-stream",
        )

    def test_upload_binary_io(self):
        """upload() passes a file-like object directly to put_object."""
        client, s3 = self._client_with_mock_s3()
        buf = io.BytesIO(b"file contents")
        client.upload("backup/file.bin", buf, content_type="application/zip")
        s3.put_object.assert_called_once_with(
            Bucket="b",
            Key="backup/file.bin",
            Body=buf,
            ContentType="application/zip",
        )

    def test_upload_error_raises_cloud_error(self):
        """CloudError raised when put_object raises."""
        client, s3 = self._client_with_mock_s3()
        s3.put_object.side_effect = Exception("upload failed")

        with pytest.raises(CloudError, match="upload failed"):
            client.upload("key", b"data")


# ---------------------------------------------------------------------------
# 4. Download
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDownload:
    def _client_with_mock_s3(self) -> tuple[Any, MagicMock]:
        boto3_mock, s3_client, _ = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            client._get_client()
        return client, s3_client

    def test_download_success(self):
        """download() returns the object body bytes."""
        client, s3 = self._client_with_mock_s3()
        body_mock = MagicMock()
        body_mock.read.return_value = b"hello world"
        s3.get_object.return_value = {"Body": body_mock}

        result = client.download("backup/db.duckdb")

        assert result == b"hello world"

    def test_download_missing_key_raises_cloud_error(self):
        """CloudError raised when key does not exist."""
        client, s3 = self._client_with_mock_s3()
        s3.get_object.side_effect = Exception("NoSuchKey")

        with pytest.raises(CloudError):
            client.download("nonexistent/key")


# ---------------------------------------------------------------------------
# 5. List objects
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListObjects:
    def _client_with_mock_s3(self) -> tuple[Any, MagicMock]:
        boto3_mock, s3_client, _ = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            client._get_client()
        return client, s3_client

    def test_list_with_prefix(self):
        """list_objects() passes prefix and returns Key/Size/LastModified dicts."""
        from datetime import datetime, timezone

        client, s3 = self._client_with_mock_s3()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "backup/2024/db.duckdb", "Size": 1024, "LastModified": ts},
            ]
        }

        result = client.list_objects(prefix="backup/")

        assert len(result) == 1
        assert result[0]["Key"] == "backup/2024/db.duckdb"
        assert result[0]["Size"] == 1024
        s3.list_objects_v2.assert_called_once_with(Bucket="b", Prefix="backup/")

    def test_list_empty_bucket(self):
        """list_objects() returns empty list when bucket has no matching objects."""
        client, s3 = self._client_with_mock_s3()
        s3.list_objects_v2.return_value = {}  # No 'Contents' key

        result = client.list_objects()

        assert result == []


# ---------------------------------------------------------------------------
# 6. Delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDelete:
    def _client_with_mock_s3(self) -> tuple[Any, MagicMock]:
        boto3_mock, s3_client, _ = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            client._get_client()
        return client, s3_client

    def test_delete_success(self):
        """delete() calls delete_object without raising."""
        client, s3 = self._client_with_mock_s3()
        client.delete("backup/old.duckdb")
        s3.delete_object.assert_called_once_with(Bucket="b", Key="backup/old.duckdb")

    def test_delete_raises_cloud_error_on_failure(self):
        """CloudError raised when delete_object raises an unexpected error."""
        client, s3 = self._client_with_mock_s3()
        s3.delete_object.side_effect = Exception("permission denied")

        with pytest.raises(CloudError):
            client.delete("backup/key")


# ---------------------------------------------------------------------------
# 7. Exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExists:
    def _client_with_s3(self, ClientError: type) -> tuple[Any, MagicMock]:
        boto3_mock, s3_client, FakeClientError = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")

        with _inject_boto3(boto3_mock):
            client._get_client()
        return client, s3_client, FakeClientError

    def test_key_exists_returns_true(self):
        """exists() returns True when head_object succeeds."""
        boto3_mock, s3_client, FakeClientError = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")
        with _inject_boto3(boto3_mock):
            client._get_client()

        s3_client.head_object.return_value = {"ContentLength": 512}
        assert client.exists("backup/db.duckdb") is True

    def test_key_missing_returns_false(self):
        """exists() returns False on a 404 ClientError."""
        boto3_mock, s3_client, FakeClientError = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")
        with _inject_boto3(boto3_mock):
            client._get_client()

        s3_client.head_object.side_effect = FakeClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        assert client.exists("nonexistent/key") is False

    def test_other_error_raises_cloud_error(self):
        """exists() re-raises non-404 errors as CloudError."""
        boto3_mock, s3_client, FakeClientError = _make_boto3_mock()
        from dango.platform.cloud.spaces import SpacesClient

        client = SpacesClient(bucket="b", region="nyc3", access_key="k", secret_key="s")
        with _inject_boto3(boto3_mock):
            client._get_client()

        s3_client.head_object.side_effect = FakeClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )
        with pytest.raises(CloudError):
            client.exists("protected/key")
