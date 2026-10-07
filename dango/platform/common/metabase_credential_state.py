"""dango/platform/common/metabase_credential_state.py

Read-only classification of the Metabase admin credential for ``dango serve``, plus the
server command that repairs it. Never repairs, retries or exposes a password.
"""

from __future__ import annotations

from pathlib import Path


def cloud_repair_admin_command() -> str:
    """Return the server command that repairs Metabase admin access, as ``dango`` (not root).

    Root would leave root-owned credential files; SSH does not inherit ``DANGO_CLOUD_MODE``.
    """
    from dango.platform.cloud.backup import PROJECT_DIR
    from dango.platform.cloud.deployer import VENV_BIN

    sudo = "sudo -u dango -H env DANGO_CLOUD_MODE=true"
    return f"cd {PROJECT_DIR} && {sudo} {VENV_BIN}/dango metabase repair-admin"


def metabase_admin_credential_state(project_root: Path, *, probe: bool = True) -> str:
    """Classify the Metabase admin credential; never repairs, retries, raises or leaks it.

    Returns ``not_configured``, ``ok``, ``missing``, ``unreadable``, ``rejected`` or
    ``unreachable`` (Metabase unhealthy: the credential is not blamed). At most one health
    GET and one login POST (the throttle counts attempts); ``probe=False`` makes no HTTP
    call and returns ``unverified`` if a credential exists.
    """
    try:
        import requests

        from dango.security.metabase_config import (
            load_metabase_admin_credentials,
            load_metabase_metadata,
            resolve_metabase_url,
        )

        metadata = load_metabase_metadata(project_root)
        admin = metadata.get("admin") if isinstance(metadata, dict) else None
        email = admin.get("email") if isinstance(admin, dict) else None
        if metadata is None or not isinstance(email, str) or not email:
            return "not_configured"
        try:
            credentials = load_metabase_admin_credentials(project_root)
        except Exception:  # noqa: BLE001
            return "unreadable"
        if credentials is None:
            return "missing"
        if not probe:
            return "unverified"
        url = resolve_metabase_url(project_root)
        if requests.get(f"{url}/api/health", timeout=5).status_code != 200:
            return "unreachable"
        login = {"username": credentials[0], "password": credentials[1]}
        status = requests.post(f"{url}/api/session", json=login, timeout=10).status_code
        return {200: "ok", 401: "rejected", 403: "rejected"}.get(status, "unreachable")
    except Exception:  # noqa: BLE001
        return "unreachable"
