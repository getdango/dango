"""tests/unit/test_web_health.py

Tests for dango.web.routes.health — platform health endpoint.

Focuses on:
- Disk warning deduplication logic in get_platform_health()
- OAuth token health: expired → critical, expiring-soon → warning
- Storage failure graceful degradation
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.web.routes.health import router


def _make_app(project_root: Path) -> FastAPI:
    """Create a minimal FastAPI app with just the health router."""
    app = FastAPI()
    app.state.project_root = project_root
    app.state.scheduler = None
    app.include_router(router)
    return app


def _base_health_data(
    disk_status: str = "healthy",
    used_pct: float = 50.0,
) -> dict[str, Any]:
    """Build a minimal health data dict with configurable disk fields."""
    return {
        "db_health": {
            "size_gb": 1,
            "size_mb": 1024,
            "tables": 10,
            "status": "healthy",
            "raw_tables": 5,
            "staging_tables": 3,
            "marts_tables": 2,
        },
        "disk": {
            "free_gb": 20,
            "total_gb": 100,
            "used_gb": 80,
            "used_pct": used_pct,
            "status": disk_status,
        },
        "disk_breakdown": {},
        "sources_config": [],
        "failed_syncs": [],
        "failed_dbt": [],
    }


@pytest.mark.unit
class TestDiskWarningDeduplication:
    """Verify the elif chain produces exactly one disk warning."""

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_critical_disk_no_warning(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Critical disk status → critical_issues only, no warnings."""
        mock_data.return_value = _base_health_data(disk_status="critical", used_pct=98)
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert "Critical disk space" in body["critical_issues"]
        disk_warnings = [w for w in body["warnings"] if "disk" in w.lower() or "80%" in w]
        assert disk_warnings == []

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_above_80pct_warning_suppresses_low_disk(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Disk at 85% with warning status → only 80% message, not "Low disk space"."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=85)
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert any("80%" in w for w in body["warnings"])
        assert "Low disk space" not in body["warnings"]

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_warning_below_80pct_shows_low_disk(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Disk in warning status but below 80% → generic "Low disk space"."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=75)
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert "Low disk space" in body["warnings"]
        assert not any("80%" in w for w in body["warnings"])

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_healthy_disk_no_warnings(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Healthy disk → no disk-related warnings."""
        mock_data.return_value = _base_health_data(disk_status="healthy", used_pct=40)
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        disk_warnings = [w for w in body["warnings"] if "disk" in w.lower() or "80%" in w]
        assert disk_warnings == []

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_exactly_80pct_shows_low_disk(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Exactly 80% → generic 'Low disk space', not the 80% actionable message."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=80.0)
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert "Low disk space" in body["warnings"]
        assert not any("80%" in w for w in body["warnings"])

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_cloud_80pct_message(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Cloud deployment → 80% message suggests resizing."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=85)
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch.dict("os.environ", {"DANGO_CLOUD_MODE": "true"}):
            resp = client.get("/api/health/platform")
        body = resp.json()

        assert any("resizing" in w for w in body["warnings"])

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_local_80pct_message(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Local deployment → 80% message suggests dango cleanup."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=85)
        # No cloud.yml → local
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert any("cleanup" in w for w in body["warnings"])
        assert not any("resizing" in w for w in body["warnings"])

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_disk_breakdown_in_response(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """disk_breakdown field is present in response."""
        data = _base_health_data()
        data["disk_breakdown"] = {"duckdb": {"file_size_mb": 100}}
        mock_data.return_value = data
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert body["disk_breakdown"] == {"duckdb": {"file_size_mb": 100}}

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    def test_missing_disk_breakdown_defaults_empty(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """If disk_breakdown is missing from data, response uses empty dict."""
        data = _base_health_data()
        del data["disk_breakdown"]
        mock_data.return_value = data
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert body["disk_breakdown"] == {}


# ---------------------------------------------------------------------------
# OAuth health in platform health response
# ---------------------------------------------------------------------------


def _make_expired_credential() -> MagicMock:
    """Create a mock OAuthCredential that is expired."""
    cred = MagicMock()
    cred.source_type = "facebook_ads"
    cred.provider = "facebook"
    cred.is_expired.return_value = True
    cred.is_expiring_soon.return_value = True
    cred.days_until_expiry.return_value = 0
    return cred


def _make_expiring_credential(days: int = 3) -> MagicMock:
    """Create a mock OAuthCredential expiring in N days."""
    cred = MagicMock()
    cred.source_type = "shopify"
    cred.provider = "shopify"
    cred.is_expired.return_value = False
    cred.is_expiring_soon.return_value = True
    cred.days_until_expiry.return_value = days
    return cred


def _make_healthy_credential() -> MagicMock:
    """Create a mock OAuthCredential with no expiry concerns."""
    cred = MagicMock()
    cred.source_type = "google_sheets"
    cred.provider = "google"
    cred.is_expired.return_value = False
    cred.is_expiring_soon.return_value = False
    cred.days_until_expiry.return_value = None
    return cred


@pytest.mark.unit
class TestOAuthHealth:
    """OAuth token health in the /api/health/platform response."""

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    @patch("dango.web.routes.health.OAuthStorage")
    def test_expired_token_in_critical_issues(
        self,
        mock_storage_cls: MagicMock,
        mock_data: AsyncMock,
        mock_cloud: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Expired OAuth token → critical_issues entry."""
        mock_data.return_value = _base_health_data()
        mock_storage_cls.return_value.list.return_value = [_make_expired_credential()]
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        oauth_criticals = [i for i in body["critical_issues"] if "OAuth" in i]
        assert len(oauth_criticals) == 1
        assert "facebook_ads" in oauth_criticals[0]
        assert body["status"] == "critical"

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    @patch("dango.web.routes.health.OAuthStorage")
    def test_expiring_soon_in_warnings(
        self,
        mock_storage_cls: MagicMock,
        mock_data: AsyncMock,
        mock_cloud: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Token expiring in 3 days → warning entry."""
        mock_data.return_value = _base_health_data()
        mock_storage_cls.return_value.list.return_value = [_make_expiring_credential(days=3)]
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        oauth_warnings = [w for w in body["warnings"] if "OAuth" in w]
        assert len(oauth_warnings) == 1
        assert "3 day" in oauth_warnings[0]
        assert "shopify" in oauth_warnings[0]
        assert body["status"] == "warning"

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    @patch("dango.web.routes.health.OAuthStorage")
    def test_no_expiry_no_warning(
        self,
        mock_storage_cls: MagicMock,
        mock_data: AsyncMock,
        mock_cloud: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Credential with no expiry → no OAuth warnings or criticals."""
        mock_data.return_value = _base_health_data()
        mock_storage_cls.return_value.list.return_value = [_make_healthy_credential()]
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        oauth_warnings = [w for w in body["warnings"] if "OAuth" in w]
        oauth_criticals = [i for i in body["critical_issues"] if "OAuth" in i]
        assert oauth_warnings == []
        assert oauth_criticals == []

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    @patch("dango.web.routes.health.OAuthStorage")
    def test_storage_failure_graceful(
        self,
        mock_storage_cls: MagicMock,
        mock_data: AsyncMock,
        mock_cloud: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """OAuthStorage failure → oauth_health is empty list, no crash."""
        mock_data.return_value = _base_health_data()
        mock_storage_cls.side_effect = RuntimeError("disk read failed")
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert resp.status_code == 200
        assert body["oauth_health"] == []

    @patch(
        "dango.web.routes.health._get_cloud_health_data", new_callable=AsyncMock, return_value=None
    )
    @patch("dango.web.routes.health.get_platform_health_data", new_callable=AsyncMock)
    @patch("dango.web.routes.health.OAuthStorage")
    def test_oauth_health_field_structure(
        self,
        mock_storage_cls: MagicMock,
        mock_data: AsyncMock,
        mock_cloud: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """oauth_health entries have expected fields."""
        mock_data.return_value = _base_health_data()
        mock_storage_cls.return_value.list.return_value = [_make_expiring_credential(days=5)]
        app = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/health/platform")
        body = resp.json()

        assert len(body["oauth_health"]) == 1
        entry = body["oauth_health"][0]
        assert entry["source_type"] == "shopify"
        assert entry["provider"] == "shopify"
        assert entry["is_expired"] is False
        assert entry["days_until_expiry"] == 5
