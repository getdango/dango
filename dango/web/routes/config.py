"""dango/web/routes/config.py

Configuration endpoints for frontend integration.
"""

import logging

import yaml
from fastapi import APIRouter

from dango.web.helpers import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["config"])


@router.get("/api/config")
async def get_config():
    """Get Dango configuration (ports, URLs, etc.).

    Returns configuration needed by the frontend to build dynamic URLs
    """
    from dango.config import ConfigLoader

    try:
        config_loader = ConfigLoader(get_project_root())
        config = config_loader.load_config()

        web_port = config.platform.port
        project_name = config.project.name
        organization = getattr(config.project, "organization", None)

        return {
            "web_port": web_port,
            "web_url": f"http://localhost:{web_port}",
            "metabase_url": "http://localhost:3000",
            "dbt_docs_url": "http://localhost:8081",
            "api_url": f"http://localhost:{web_port}/api",
            "project_name": project_name,
            "organization": organization,
        }
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        # Return defaults if config fails to load
        return {
            "web_port": 8800,
            "web_url": "http://localhost:8800",
            "metabase_url": "http://localhost:3000",
            "dbt_docs_url": "http://localhost:8081",
            "api_url": "http://localhost:8800/api",
            "project_name": "Unknown Project",
            "organization": None,
        }


@router.get("/api/metabase-config")
async def get_metabase_config():
    """Get Metabase configuration including database ID.

    Returns:
        Dictionary with Metabase configuration
    """
    try:
        metabase_yml_path = get_project_root() / ".dango" / "metabase.yml"

        if not metabase_yml_path.exists():
            return {"database_id": None, "configured": False}

        with open(metabase_yml_path, encoding="utf-8") as f:
            metabase_config = yaml.safe_load(f)

        database_id = metabase_config.get("database", {}).get("id")

        return {"database_id": database_id, "configured": True}
    except Exception as e:
        logger.error(f"Failed to load Metabase config: {e}")
        return {"database_id": None, "configured": False}
