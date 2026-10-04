"""dango/platform/common/metabase_admin_repair.py

Automatic, data-preserving repair of a lost Metabase admin credential (1.0.12 / R4).

When every stored copy of the Metabase admin password is rejected, Metabase's own offline
``reset-password`` CLI is the supported way back in: it needs no old password and keeps all
data (the same trusted-operator model as the container itself). The flow is:

1. stage a fresh credential in the protected store's pending slot (crash-safe),
2. stop Metabase (the CLI needs the H2 app DB), run the one-off CLI, restart Metabase
   (always, in ``finally``),
3. redeem the one-time token over HTTP, verify a login with the new password,
4. promote the pending credential and reuse the migration's SSO refresh + YAML cleanup.

Nothing here deletes a volume. Any failure leaves the previous state untouched, retains the
Metabase container running, and is reported with a secret-free reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.platform.common import metabase_credential_migration as _migration
from dango.security.metabase_config import load_metabase_metadata
from dango.security.metabase_credentials import MetabaseCredentialStore

_STATE_FILENAME = "metabase_admin_repair.json"
_RETRY_AFTER_SECONDS = 3600
_TOKEN_PATTERN = re.compile(r"OK \[\[\[(.+?)\]\]\]")
_CLI_TIMEOUT_SECONDS = 300


def repair_admin_credential(project_root: Path, *, ready_timeout: int = 180) -> dict[str, object]:
    """Regain Metabase admin access without touching Metabase data.

    Returns ``{"status": "repaired"}`` or ``{"status": "failed", "reason": <slug>}`` /
    ``{"status": "skipped", "reason": <slug>}``. Never raises; never includes a secret.
    """
    root = Path(project_root)
    try:
        return _repair(root, ready_timeout)
    except Exception:
        return _failed(root, "unexpected_error")


def _repair(root: Path, ready_timeout: int) -> dict[str, object]:
    metadata = load_metabase_metadata(root)
    admin = metadata.get("admin") if isinstance(metadata, dict) else None
    admin_email = admin.get("email") if isinstance(admin, dict) else None
    metabase_url = (
        metadata.get("metabase_url", "http://localhost:3000")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(admin_email, str) or not admin_email:
        return {"status": "skipped", "reason": "missing_admin_email"}
    if not isinstance(metabase_url, str) or not metabase_url:
        return {"status": "skipped", "reason": "missing_metabase_url"}
    metabase_url = metabase_url.rstrip("/")
    if not (root / "docker-compose.yml").exists():
        return {"status": "skipped", "reason": "no_local_compose"}
    if _attempted_recently(root):
        return {"status": "skipped", "reason": "attempted_recently"}

    from dango.auth.metabase_sync import generate_metabase_password
    from dango.platform.docker import get_compose_project_name
    from dango.visualization.metabase import wait_for_metabase_ready

    with _migration._migration_lock(root):
        store = MetabaseCredentialStore(root)
        new_password = generate_metabase_password()
        try:
            store.save_pending(new_password)
        except Exception:
            return _failed(root, "candidate_staging_failed")

        env = {**os.environ, "COMPOSE_PROJECT_NAME": get_compose_project_name(root)}
        token: str | None = None
        try:
            _compose(root, env, "stop", "metabase", timeout=90)
            token = _run_reset_cli(root, env, admin_email)
        finally:
            # Always bring Metabase back, whatever happened above.
            _compose(root, env, "up", "-d", "metabase", timeout=120)
        if token is None:
            _discard_pending(store)
            return _failed(root, "reset_cli_failed")
        if not wait_for_metabase_ready(metabase_url, timeout=ready_timeout):
            # The token stays valid and the pending credential is retained; a later start
            # re-runs the whole flow, which mints a fresh token.
            return _failed(root, "metabase_not_ready")

        if not _redeem_token(metabase_url, token, new_password):
            _discard_pending(store)
            return _failed(root, "token_not_accepted")
        session = _migration._create_session(metabase_url, admin_email, new_password)
        if session is None:
            return _failed(root, "new_credential_not_accepted")
        try:
            active = store.promote_pending()
        except Exception:
            return _failed(root, "pending_promotion_failed")
        result = _migration._refresh_sso_and_cleanup(
            root, metadata or {}, metabase_url, session, admin_email, active
        )
        if result.get("status") != "secure_rotated":
            # The new credential is promoted and works; only the follow-up tidy-up failed.
            return _failed(root, f"followup_{result.get('reason', 'failed')}")
        _clear_state(root)
        return {"status": "repaired"}


def _compose(root: Path, env: dict[str, str], *args: str, timeout: int) -> None:
    subprocess.run(
        ["docker", "compose", *args],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_reset_cli(root: Path, env: dict[str, str], admin_email: str) -> str | None:
    """Run Metabase's offline ``reset-password`` and return its one-time token."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "run", "--rm", "--no-deps", "-T", "metabase"]
            + ["reset-password", admin_email],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    match = _TOKEN_PATTERN.search(proc.stdout or "")
    return match.group(1) if match else None


def _redeem_token(metabase_url: str, token: str, password: str) -> bool:
    import requests

    try:
        response = requests.post(
            f"{metabase_url}/api/session/reset_password",
            json={"token": token, "password": password},
            timeout=15,
        )
    except Exception:
        return False
    return response.status_code == 200


def _discard_pending(store: MetabaseCredentialStore) -> None:
    try:
        store.discard_pending()
    except Exception:  # noqa: BLE001 - keep the original failure reason
        pass


def _state_path(root: Path) -> Path:
    return root / ".dango" / "state" / _STATE_FILENAME


def _attempted_recently(root: Path) -> bool:
    try:
        data: Any = json.loads(_state_path(root).read_text())
        then = datetime.fromisoformat(data["at"])
    except Exception:
        return False
    if then.tzinfo is None:
        return False
    return 0 <= (datetime.now(timezone.utc) - then).total_seconds() < _RETRY_AFTER_SECONDS


def _failed(root: Path, reason: str) -> dict[str, object]:
    state: dict[str, object] = {
        "status": "failed",
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path = _state_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass
    return {"status": "failed", "reason": reason}


def _clear_state(root: Path) -> None:
    try:
        _state_path(root).unlink()
    except OSError:
        pass
