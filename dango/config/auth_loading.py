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

#: Designed cloud session limits (must match ``_build_auth_timeout_script`` in
#: ``deploy_provision``); re-exported by ``platform/cloud/server_auth.py``.
CLOUD_AUTH_TIMEOUTS: dict[str, int] = {"session_max_days": 30, "idle_timeout_minutes": 60}

# (file, error, cloud) combinations already warned about; the loader is called from
# create_app, lifespan and per-request route helpers, so warn once per distinct problem.
_warned: set[tuple[str, str, bool]] = set()


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
        return AuthConfig.model_validate(CLOUD_AUTH_TIMEOUTS)
    return None


def _warn_once(file: Path, field: str | None, message: str) -> None:
    cloud = is_running_on_cloud()
    key = (str(file), message, cloud)
    if key in _warned:
        return
    _warned.add(key)
    fallback: dict[str, Any] = {"fallback": "local_defaults"}
    if cloud:
        fallback = {"fallback": "cloud_defaults", **CLOUD_AUTH_TIMEOUTS}
    logger.warning("auth_config_invalid", file=str(file), field=field, error=message, **fallback)


def load_auth_config_safe(project_root: Path | None) -> AuthConfig | None:
    """Load ``auth:`` from project.yml.

    Returns None when there is no project config (or on local-mode failure, so
    callers use the local defaults). On a cloud server a failure, or a missing
    project.yml, returns an AuthConfig with the cloud timeouts. Problems are
    logged at WARNING (once per distinct problem per process).
    """
    loader = ConfigLoader(project_root or Path.cwd())
    project_file = loader.project_file
    if not project_file.exists():
        if is_running_on_cloud():
            _warn_once(project_file, None, "project.yml not found")
        else:
            logger.debug("auth_config_not_loaded", reason="no project config found, using defaults")
        return _fallback()

    try:
        data: Any = yaml.safe_load(project_file.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("project.yml top level must be a mapping")
        return AuthConfig(**data.get("auth", {}))
    except Exception as exc:
        message, field = _describe_error(exc)
        _warn_once(project_file, field, message)
        return _fallback()
