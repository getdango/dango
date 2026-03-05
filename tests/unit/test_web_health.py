"""tests/unit/test_web_health.py

Tests for dango.web.routes.health — platform health endpoint.

Focuses on the disk warning deduplication logic in get_platform_health():
- Critical disk → critical_issues (no duplicate warning)
- Disk >80% used → actionable 80% warning (suppresses "Low disk space")
- Disk warning but <80% → generic "Low disk space"
- Cloud vs local message variants
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
    def test_cloud_80pct_message(
        self, mock_data: AsyncMock, mock_cloud: AsyncMock, tmp_path: Path
    ) -> None:
        """Cloud deployment → 80% message suggests resizing."""
        mock_data.return_value = _base_health_data(disk_status="warning", used_pct=85)
        # Create cloud.yml to simulate cloud deployment
        (tmp_path / ".dango").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".dango" / "cloud.yml").write_text("droplet_id: 123\n")
        app = _make_app(tmp_path)
        client = TestClient(app)

        with patch("dango.web.routes.health.get_project_root", return_value=tmp_path):
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
