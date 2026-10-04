"""dango/ingestion/credential_health.py

Cross-references configured sources against available credentials (OAuth
tokens, API-key env vars) and reports missing/expired/expiring issues.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
_CACHE_TTL = 300  # 5 minutes


def _stored_block(source: Any, source_type: str) -> dict[str, Any]:
    """The source's stored type-config values (typed block or generic_config) as a dict."""
    from pydantic import BaseModel

    from dango.config.models import DataSource

    block = (
        getattr(source, source_type, None)
        if source_type in DataSource.model_fields
        else getattr(source, "generic_config", None)
    )
    if isinstance(block, BaseModel):
        return block.model_dump(mode="json", exclude_none=True)
    return dict(block) if isinstance(block, dict) else {}


def run_credential_checks(project_root: Path) -> list[dict[str, Any]]:
    """Check all configured sources for credential health.

    Returns list of dicts:
      {"source": str, "type": str, "auth_type": str,
       "status": "ok"|"missing"|"expired"|"expiring_soon"|"unknown",
       "detail": str}
    """
    from dango.config.helpers import get_config
    from dango.ingestion.sources.registry import AuthType, get_source_metadata
    from dango.ingestion.sources.setup_schema import (
        _is_secret_param,
        compute_env_var_name,
        is_env_var_name,
    )
    from dango.oauth.storage import OAuthStorage
    from dango.oauth.validation import validate_token

    results: list[dict[str, Any]] = []
    config = get_config(project_root)
    sources = config.sources.sources  # SourcesConfig.sources — list[DataSource]
    if not sources:
        return results

    secrets_file = project_root / ".dlt" / "secrets.toml"
    # OAuthStorage creates an empty secrets.toml on construction; a read-only check must not.
    oauth_storage = OAuthStorage(project_root) if secrets_file.exists() else None
    env_file = project_root / ".env"
    dot_env = {}
    if env_file.exists():
        from dotenv import dotenv_values

        dot_env = dotenv_values(env_file)

    for source in sources:
        source_type = source.type.value
        source_name = source.name
        registry_entry = get_source_metadata(source_type) or {}
        auth_type = registry_entry.get("auth_type", AuthType.NONE)

        if auth_type == AuthType.OAUTH:
            cred = oauth_storage.get(source_type) if oauth_storage is not None else None
            if cred is None:
                results.append(
                    {
                        "source": source_name,
                        "type": source_type,
                        "auth_type": "oauth",
                        "status": "missing",
                        "detail": f"No OAuth credential found. Run 'dango oauth {source_type}'",
                    }
                )
                continue
            try:
                vr = validate_token(cred)
                if not vr.valid:
                    results.append(
                        {
                            "source": source_name,
                            "type": source_type,
                            "auth_type": "oauth",
                            "status": "expired",
                            "detail": vr.message,
                        }
                    )
                elif cred.is_expiring_soon(days=7):
                    days = cred.days_until_expiry()
                    results.append(
                        {
                            "source": source_name,
                            "type": source_type,
                            "auth_type": "oauth",
                            "status": "expiring_soon",
                            "detail": f"Expires in {days} day(s)",
                        }
                    )
                else:
                    results.append(
                        {
                            "source": source_name,
                            "type": source_type,
                            "auth_type": "oauth",
                            "status": "ok",
                            "detail": cred.account_info or "",
                        }
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "Failed to validate token for %s: %s",
                    source_type,
                    e,
                    exc_info=True,
                )
                results.append(
                    {
                        "source": source_name,
                        "type": source_type,
                        "auth_type": "oauth",
                        "status": "unknown",
                        "detail": "Failed to validate token",
                    }
                )

        elif auth_type in (AuthType.API_KEY, AuthType.BASIC):
            required = registry_entry.get("required_params", [])
            stored = _stored_block(source, source_type)
            # Each source's own variable: the name stored in its config, else the computed one
            # (same rule as setup_lifecycle.credential_requirements).
            env_names: list[str] = []
            invalid: list[str] = []
            required_names = {r["name"] for r in required}
            for p in required + registry_entry.get("optional_params", []):
                if not _is_secret_param(p):
                    continue
                if p["name"] not in required_names and p["name"] not in stored:
                    continue
                name = str(stored.get(p["name"]) or compute_env_var_name(p, source_name))
                if is_env_var_name(name):
                    env_names.append(name)
                else:  # never echo a stored value that is not a variable name (may be a literal)
                    invalid.append(p["name"])
            if not env_names and not invalid:
                results.append(
                    {
                        "source": source_name,
                        "type": source_type,
                        "auth_type": auth_type.value,
                        "status": "ok",
                        "detail": "",
                    }
                )
                continue
            missing = [n for n in env_names if not os.environ.get(n) and not dot_env.get(n)]
            problems = [*missing, *(f"{p} (not a valid env var name)" for p in invalid)]
            results.append(
                {
                    "source": source_name,
                    "type": source_type,
                    "auth_type": auth_type.value,
                    "status": "ok" if not problems else "missing",
                    "detail": "" if not problems else f"Missing: {', '.join(problems)}",
                }
            )

        elif auth_type == AuthType.SERVICE_ACCOUNT:
            # Check if secrets.toml exists and contains the source-specific section
            found = False
            detail = ""
            if secrets_file.exists() and secrets_file.stat().st_size > 0:
                try:
                    import toml

                    secrets = toml.load(secrets_file)
                    # Service account credentials should be under sources.{source_type}
                    source_secrets = secrets.get("sources", {}).get(source_type, {})
                    found = bool(source_secrets)
                    if not found:
                        detail = f"No [sources.{source_type}] section in .dlt/secrets.toml"
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        "Failed to parse secrets.toml for %s: %s",
                        source_type,
                        e,
                    )
                    detail = "Failed to parse .dlt/secrets.toml"
            else:
                detail = "No .dlt/secrets.toml found — add service-account credentials there"
            results.append(
                {
                    "source": source_name,
                    "type": source_type,
                    "auth_type": "service_account",
                    "status": "ok" if found else "missing",
                    "detail": detail,
                }
            )

        else:
            results.append(
                {
                    "source": source_name,
                    "type": source_type,
                    "auth_type": auth_type.value,
                    "status": "ok",
                    "detail": "",
                }
            )

    return results


def get_cached_credential_health(
    project_root: Path, *, refresh: bool = False
) -> list[dict[str, Any]]:
    """Return cached results or run a fresh check (5-minute TTL, in-process only).

    ``refresh=True`` bypasses the cache and repopulates it.

    Cache is scoped per project_root to handle multi-project scenarios.
    """
    global _cache
    cache_key = str(project_root.resolve())
    now = time.monotonic()
    if not refresh and cache_key in _cache and (now - _cache[cache_key][1]) < _CACHE_TTL:
        return _cache[cache_key][0]
    results = run_credential_checks(project_root)
    _cache[cache_key] = (results, now)
    return results
