"""dango/platform/cloud/spaces.py

DigitalOcean Spaces client for Dango cloud backups.

DO Spaces is S3-compatible; this client uses ``boto3`` (core dependency,
``pip install getdango``) to interact with it.  boto3 is lazy-imported
to keep CLI startup fast.

Authentication
--------------
Credentials are read from environment variables whose names are stored in
``SpacesConfig.access_key_env`` / ``SpacesConfig.secret_key_env`` (defaults:
``SPACES_ACCESS_KEY`` / ``SPACES_SECRET_KEY``).  You can override them by
passing ``access_key`` / ``secret_key`` directly to the constructor.
"""

from __future__ import annotations

import os
from typing import IO, Any

from dango.exceptions import CloudAuthError, CloudError

# boto3 / botocore are imported lazily inside _get_client().
# Lazy-imported to keep CLI startup fast.


class SpacesClient:
    """S3-compatible client for DigitalOcean Spaces.

    Args:
        bucket: Spaces bucket name.
        region: DO region where the bucket lives (e.g. ``"nyc3"``).
        access_key: Spaces access key. Reads from ``access_key_env`` env var
            if not provided.
        secret_key: Spaces secret key. Reads from ``secret_key_env`` env var
            if not provided.
        access_key_env: Name of the env var that holds the access key.
            Default: ``"SPACES_ACCESS_KEY"``.
        secret_key_env: Name of the env var that holds the secret key.
            Default: ``"SPACES_SECRET_KEY"``.

    Raises:
        CloudAuthError: If credentials cannot be resolved from arguments or
            environment variables.
    """

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        access_key_env: str = "SPACES_ACCESS_KEY",
        secret_key_env: str = "SPACES_SECRET_KEY",
    ) -> None:
        self.bucket = bucket
        self.region = region

        resolved_key = access_key or os.environ.get(access_key_env)
        resolved_secret = secret_key or os.environ.get(secret_key_env)

        if not resolved_key:
            raise CloudAuthError(
                f"No Spaces access key found. Set the {access_key_env} environment "
                "variable or pass access_key= to SpacesClient.",
            )
        if not resolved_secret:
            raise CloudAuthError(
                f"No Spaces secret key found. Set the {secret_key_env} environment "
                "variable or pass secret_key= to SpacesClient.",
            )

        self._access_key = resolved_key
        self._secret_key = resolved_secret
        self._s3_client: Any = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a cached boto3 S3 client configured for Spaces.

        Raises:
            CloudError: If boto3 is not installed.
        """
        if self._s3_client is not None:
            return self._s3_client

        try:
            import boto3  # type: ignore[import]
        except ImportError:
            raise CloudError(
                "boto3 is required for Spaces operations. Reinstall with: pip install getdango"
            ) from None

        endpoint = f"https://{self.region}.digitaloceanspaces.com"
        self._s3_client = boto3.client(
            "s3",
            region_name=self.region,
            endpoint_url=endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )
        return self._s3_client

    def _wrap_client_error(self, exc: Exception, operation: str, key: str) -> CloudError:
        """Convert a boto3 ClientError into a CloudError with a readable message."""
        return CloudError(
            f"Spaces {operation} failed for key '{key}': {exc}",
            context={"bucket": self.bucket, "key": key, "operation": operation},
        )

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    def upload(
        self,
        key: str,
        data: bytes | IO[bytes],
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload an object to Spaces.

        Args:
            key: Object key (path within the bucket).
            data: Raw bytes or a binary file-like object to upload.
            content_type: MIME type for the object. Default: ``application/octet-stream``.
        """
        client = self._get_client()
        try:
            client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:
            _reraise_if_not_client_error(exc)
            raise self._wrap_client_error(exc, "upload", key) from exc

    def download(self, key: str) -> bytes:
        """Download an object from Spaces and return its contents.

        Args:
            key: Object key to download.

        Returns:
            Object contents as bytes.

        Raises:
            CloudError: If the key does not exist or another error occurs.
        """
        client = self._get_client()
        try:
            response: dict[str, Any] = client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()  # type: ignore[no-any-return]
        except Exception as exc:
            _reraise_if_not_client_error(exc)
            raise self._wrap_client_error(exc, "download", key) from exc

    def list_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """List objects in the bucket with an optional key prefix.

        Paginates through all pages (``list_objects_v2`` returns at most 1000
        objects per call; subsequent pages are fetched via ``NextContinuationToken``).

        Args:
            prefix: Only objects whose key starts with this string are returned.

        Returns:
            List of dicts with keys ``Key``, ``Size``, and ``LastModified``.
        """
        client = self._get_client()
        results: list[dict[str, Any]] = []
        continuation_token: str | None = None

        while True:
            params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                params["ContinuationToken"] = continuation_token

            try:
                response: dict[str, Any] = client.list_objects_v2(**params)
            except Exception as exc:
                _reraise_if_not_client_error(exc)
                raise self._wrap_client_error(exc, "list", prefix) from exc

            for obj in response.get("Contents", []):
                results.append(
                    {
                        "Key": obj["Key"],
                        "Size": obj["Size"],
                        "LastModified": obj["LastModified"],
                    }
                )

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        return results

    def delete(self, key: str) -> None:
        """Delete an object from Spaces.

        Idempotent — no error is raised if the key does not exist.

        Args:
            key: Object key to delete.
        """
        client = self._get_client()
        try:
            client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            _reraise_if_not_client_error(exc)
            raise self._wrap_client_error(exc, "delete", key) from exc

    def delete_bucket(self) -> None:
        """Delete the Spaces bucket.

        The bucket must be empty first — call ``delete()`` on every object
        before calling this method.

        Raises:
            CloudError: If the bucket cannot be deleted (not empty, permissions, etc.).
        """
        client = self._get_client()
        try:
            client.delete_bucket(Bucket=self.bucket)
        except Exception as exc:
            _reraise_if_not_client_error(exc)
            raise self._wrap_client_error(exc, "delete_bucket", self.bucket) from exc

    def exists(self, key: str) -> bool:
        """Check whether an object key exists in the bucket.

        Args:
            key: Object key to check.

        Returns:
            ``True`` if the object exists, ``False`` if it does not.

        Raises:
            CloudError: On unexpected errors (permissions, network, etc.).
        """
        client = self._get_client()
        try:
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            # boto3 raises ClientError with code "404" for missing objects
            # (head_object uses the HTTP status code as the error code).
            # Re-raise non-ClientError exceptions immediately.
            _reraise_if_not_client_error(exc)
            error_code = _get_boto_error_code(exc)
            if error_code in ("404", "NoSuchKey"):
                return False
            raise self._wrap_client_error(exc, "exists", key) from exc


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _reraise_if_not_client_error(exc: Exception) -> None:
    """Re-raise *exc* if it is not a boto3 ``ClientError``.

    boto3 ``ClientError`` instances carry a ``response`` attribute with an
    ``"Error"`` dict.  Any exception without this structure is a programming
    error (``TypeError``, ``AttributeError``, etc.) and should propagate
    unchanged rather than being silently wrapped in ``CloudError``.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict) or "Error" not in response:
        raise exc


def _get_boto_error_code(exc: Exception) -> str:
    """Extract the HTTP status code string from a boto3 ClientError.

    Returns an empty string if the exception is not a ClientError or the
    error response cannot be parsed.
    """
    try:
        response: dict[str, Any] = getattr(exc, "response", {})
        return str(response["Error"]["Code"])
    except (AttributeError, KeyError, TypeError):
        return ""
