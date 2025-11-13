"""
Metabase Dashboard Integration

Provides tools for creating and provisioning Metabase dashboards.
"""

from .metabase import provision_dashboard, create_pipeline_health_dashboard

__all__ = ["provision_dashboard", "create_pipeline_health_dashboard"]
