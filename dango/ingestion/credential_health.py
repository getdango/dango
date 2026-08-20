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


def run_credential_checks(project_root: Path) -> list[dict[str, Any]]:
    """Check all configured sources for credential health.

    Returns list of dicts:
      {"source": str, "type": str, "auth_type": str,
       "status": "ok"|"missing"|"expired"|"expiring_soon"|"unknown",
       "detail": str}
    """
    from dango.config.helpers import get_config
    from dango.ingestion.sources.registry import AuthType, get_source_metadata
    from dango.oauth.storage import OAuthStorage
    from dango.oauth.validation import validate_token

    results: list[dict[str, Any]] = []
    config = get_config(project_root)
    sources = config.sources.sources  # SourcesConfig.sources — list[DataSource]
    if not sources:
        return results

    oauth_storage = OAuthStorage(project_root)
    env_file = project_root / ".env"
    dot_env = {}
    if env_file.exists():
        from dotenv import dotenv_values

        dot_env = dotenv_values(env_file)

    secrets_file = project_root / ".dlt" / "secrets.toml"

    for source in sources:
        source_type = source.type.value
        source_name = source.name
        registry_entry = get_source_metadata(source_type) or {}
        auth_type = registry_entry.get("auth_type", AuthType.NONE)

        if auth_type == AuthType.OAUTH:
            cred = oauth_storage.get(source_type)
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
            params = registry_entry.get("required_params", []) + registry_entry.get(
                "optional_params", []
            )
            secret_params = [p for p in params if p.get("type") == "secret" and p.get("env_var")]
            if not secret_params:
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
            missing = [
                p["env_var"]
                for p in secret_params
                if not os.environ.get(p["env_var"]) and p["env_var"] not in dot_env
            ]
            results.append(
                {
                    "source": source_name,
                    "type": source_type,
                    "auth_type": auth_type.value,
                    "status": "ok" if not missing else "missing",
                    "detail": "" if not missing else f"Missing: {', '.join(missing)}",
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


def get_cached_credential_health(project_root: Path) -> list[dict[str, Any]]:
    """Return cached results or run a fresh check (5-minute TTL, in-process only).

    Cache is scoped per project_root to handle multi-project scenarios.
    """
    global _cache
    cache_key = str(project_root.resolve())
    now = time.monotonic()
    if cache_key in _cache and (now - _cache[cache_key][1]) < _CACHE_TTL:
        return _cache[cache_key][0]
    results = run_credential_checks(project_root)
    _cache[cache_key] = (results, now)
    return results
