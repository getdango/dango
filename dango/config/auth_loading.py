"""dango/config/auth_loading.py

Fail-safe loading of the ``auth:`` section of project.yml for the web server.

An invalid ``auth:`` section must not crash the server (availability), but it
must also never silently weaken the session timeouts: failures are logged at
WARNING and, on a cloud server, the cloud timeouts are applied.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from dango.config.helpers import is_running_on_cloud
from dango.config.loader import ConfigLoader
from dango.config.models import AuthConfig
from dango.logging import get_logger

logger = get_logger(__name__)

# Designed cloud session limits (match the values provisioned on cloud servers).
CLOUD_IDLE_TIMEOUT_MINUTES = 60
CLOUD_SESSION_MAX_DAYS = 30


def _describe_error(exc: Exception) -> tuple[str, str | None]:
    """Return (message, field) without echoing input values (may be secrets)."""
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        fields = [".".join(str(p) for p in e["loc"]) for e in errors]
        msgs = [f"{f}: {e['msg']}" for f, e in zip(fields, errors, strict=True)]
        return "; ".join(msgs), ", ".join(fields) or None
    if isinstance(exc, yaml.YAMLError):
        return "invalid YAML", None
    return f"{type(exc).__name__}: {exc}", None


def _fallback() -> AuthConfig | None:
    if is_running_on_cloud():
        return AuthConfig(
            idle_timeout_minutes=CLOUD_IDLE_TIMEOUT_MINUTES,
            session_max_days=CLOUD_SESSION_MAX_DAYS,
        )
    return None


def load_auth_config_safe(project_root: Path | None) -> AuthConfig | None:
    """Load ``auth:`` from project.yml.

    Returns None when there is no project config (or on local-mode failure, so
    callers use the local defaults). On a cloud server a failure returns an
    AuthConfig with the cloud timeouts. Failures are logged at WARNING.
    """
    loader = ConfigLoader(project_root or Path.cwd())
    project_file = loader.project_file
    if not project_file.exists():
        logger.debug("auth_config_not_loaded", reason="no project config found, using defaults")
        return None

    try:
        data: Any = yaml.safe_load(project_file.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("project.yml top level must be a mapping")
        return AuthConfig(**data.get("auth", {}))
    except Exception as exc:
        message, field = _describe_error(exc)
        cloud = is_running_on_cloud()
        logger.warning(
            "auth_config_invalid",
            file=str(project_file),
            field=field,
            error=message,
            fallback="cloud_defaults" if cloud else "local_defaults",
            idle_timeout_minutes=(CLOUD_IDLE_TIMEOUT_MINUTES if cloud else None),
            session_max_days=(CLOUD_SESSION_MAX_DAYS if cloud else None),
        )
        return _fallback()
